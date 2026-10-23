"""Cloudflare Python Worker entry point for Rebellion.

Adapts the runtime to the runtime-neutral app: a D1 wrapper with async all/first/run, an async
http(method, url, headers, body) on top of fetch, and a Config read from env on every request
(so a secret set later, such as GITHUB_CLIENT_SECRET, works without a redeploy).
"""
import asyncio
import time
from urllib.parse import parse_qsl, urlsplit

from js import Headers, Object
from js import Response as JsResponse
from js import caches
from js import fetch as js_fetch
from pyodide.ffi import to_js
from workers import WorkerEntrypoint

import app
import store

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
# Edge cache for anonymous GETs (no session cookie, no Authorization). Never for anything that sets a cookie.
NO_CACHE_PREFIXES = ("/auth/", "/me", "/admin", "/signin", "/healthz", "/logout")
STATIC_PATHS = ("/security", "/rules", "/agents", "/agents.md", "/llms.txt", "/comms", "/comms.md", "/security.md",
                "/rules.md", "/robots.txt", "/client/rebellion_client.py", "/contact")
PAGE_CACHE = "public, max-age=0, s-maxage=30, stale-while-revalidate=300"
STATIC_CACHE = "public, max-age=300, s-maxage=3600, stale-while-revalidate=86400"


def _py(value):
    """A JS value (object, array, null) as plain Python."""
    if value is None:
        return None
    if hasattr(value, "to_py"):
        return value.to_py()
    return value


def _arg(value):
    if value is None:
        raise ValueError("refusing to bind NULL: every column has a default")
    if isinstance(value, bool):
        return int(value)
    return value


class D1:
    """async all/first/run over a D1 binding, returning dicts (the contract store.py expects)."""

    def __init__(self, binding):
        self.binding = binding

    def _stmt(self, sql, args):
        stmt = self.binding.prepare(sql)
        return stmt.bind(*[_arg(a) for a in args]) if args else stmt

    async def all(self, sql, *args):
        res = _py(await self._stmt(sql, args).all()) or {}
        return [dict(row) for row in (res.get("results") or [])]

    async def first(self, sql, *args):
        row = _py(await self._stmt(sql, args).first())
        return dict(row) if row else None

    async def run(self, sql, *args):
        res = _py(await self._stmt(sql, args).run()) or {}
        meta = res.get("meta") or {}
        return {"changes": int(meta.get("changes") or 0), "last_row_id": int(meta.get("last_row_id") or 0)}


async def http(method, url, headers, body):
    init = {"method": method, "headers": dict(headers or {})}
    if body is not None:
        init["body"] = body
    resp = await js_fetch(url, to_js(init, dict_converter=Object.fromEntries))
    return int(resp.status), str(await resp.text())


def env_str(env, name):
    try:
        value = getattr(env, name)
    except AttributeError:
        return ""
    if value is None:
        return ""
    return str(value)


def config(env, host):
    admins = tuple(a.strip().lower() for a in env_str(env, "ADMIN_TEAMS").split(",") if a.strip())
    local = host.split(":")[0] in LOCAL_HOSTS or host.startswith("[::1]")
    return store.Config(
        session_secret=env_str(env, "SESSION_SECRET"),
        oauth_client_id=env_str(env, "GITHUB_CLIENT_ID"),
        oauth_client_secret=env_str(env, "GITHUB_CLIENT_SECRET"),
        admin_teams=admins or ("lurrobert",),
        public_origin="" if local else env_str(env, "PUBLIC_ORIGIN"),
        messaging_enabled=env_str(env, "MESSAGING").lower() != "off",
        extras_enabled=env_str(env, "EXTRAS").lower() == "on",
        agentmail_key=env_str(env, "AGENTMAIL_API_KEY"),
        notify_to=env_str(env, "NOTIFY_TO"),
    )


def _headers(request):
    out = {}
    raw = request.headers
    if hasattr(raw, "items"):
        pairs = raw.items()
    else:
        pairs = ((p[0], p[1]) for p in raw.entries())
    for key, value in pairs:
        out[str(key).lower()] = str(value)
    return out


def _response(resp):
    headers = Headers.new()
    for key, value in resp.headers:
        headers.append(key, value)
    init = to_js({"status": int(resp.status), "headers": headers}, dict_converter=Object.fromEntries)
    no_body = resp.status in (101, 204, 205, 304) or not resp.body
    return JsResponse.new(None if no_body else resp.body, init)


def cache_policy(method, path, headers):
    """The Cache-Control for an anonymous response, or '' when the request must not touch the cache."""
    if method != "GET" or headers.get("authorization") or "rb_session=" in headers.get("cookie", ""):
        return ""
    if path.startswith(NO_CACHE_PREFIXES):
        return ""
    if path in STATIC_PATHS or path.startswith("/static/"):
        return STATIC_CACHE
    return PAGE_CACHE


class Default(WorkerEntrypoint):
    def _defer(self, coro):
        """Runs a coroutine after the response is sent (a Python future passes to JS as a Promise)."""
        self.ctx.waitUntil(asyncio.ensure_future(coro))

    async def fetch(self, request):
        url = urlsplit(str(request.url))
        method = str(request.method).upper()
        headers = _headers(request)
        policy = cache_policy(method, url.path or "/", headers)
        cache = caches.default
        if policy:
            hit = await cache.match(request.url)
            if hit:
                return hit
        body = ""
        if method not in ("GET", "HEAD"):
            body = str(await request.text() or "")
        req = app.Req(
            method=method,
            path=url.path or "/",
            query=dict(parse_qsl(url.query, keep_blank_values=False)),
            headers=headers,
            body=body,
            scheme=url.scheme or "https",
            host=url.netloc or "localhost",
        )
        ctx = store.Ctx(db=D1(self.env.DB), http=http, now=int(time.time()), config=config(self.env, req.host),
                        defer=self._defer)
        resp = await app.handle(req, ctx)
        if policy and resp.status == 200 and not any(k.lower() == "set-cookie" for k, _ in resp.headers):
            resp.headers = [(k, v) for k, v in resp.headers if k.lower() != "cache-control"]
            resp.headers.append(("Cache-Control", policy))
            out = _response(resp)
            self.ctx.waitUntil(cache.put(request.url, out.clone()))
            return out
        return _response(resp)

    async def scheduled(self, controller, env=None, ctx=None):
        """Cron: refresh the leaderboard cache off the request path and prune old rate counters."""
        now = int(time.time())
        sctx = store.Ctx(db=D1(self.env.DB), http=http, now=now, config=config(self.env, "cron"))
        await store.refresh_boards(sctx)
        await sctx.db.run("DELETE FROM rate_counters WHERE win < ?", now - 2 * 86400)

