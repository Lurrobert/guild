"""Test doubles: sqlite3 in place of D1, a fake http for GitHub and the leaderboard, and a Board
that drives app.handle() in-process (humans with cookies, agents with Bearer keys)."""
import asyncio
import json
import os
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit

import app
import core
import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "s" * 64
T0 = 1_792_740_000


class SqliteDB:
    """The async all/first/run contract of the D1 wrapper, on sqlite3 with the real schema."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with open(os.path.join(ROOT, "schema.sql")) as handle:
            self.conn.executescript(handle.read())

    @staticmethod
    def _check(args):
        for value in args:
            if value is None:
                raise ValueError("NULL bound: D1 in Pyodide would get undefined")

    async def all(self, sql, *args):
        self._check(args)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    async def first(self, sql, *args):
        self._check(args)
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    async def run(self, sql, *args):
        self._check(args)
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return {"changes": cur.rowcount, "last_row_id": cur.lastrowid}

    def dump(self):
        """Every cell of every table, as text (for 'no plaintext stored' checks)."""
        out = []
        tables = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for row in self.conn.execute(f"SELECT * FROM {table}"):
                out.extend(str(v) for v in tuple(row))
        return "\n".join(out)


def board_rows(teams):
    """Leaderboard JSON bodies: overall + every problem board, with the given teams ranked."""
    bodies = {"overall": json.dumps({"board": "overall", "rows": [
        {"rank": i + 1, "team": t, "solved": 4} for i, t in enumerate(teams)]})}
    for n, board in enumerate(core.PROBLEMS):
        bodies[board] = json.dumps({"board": board, "rows": [
            {"job": f"{i:04x}{n:04x}abcdef12", "team": t, "speedup": 2.0 - i * 0.1} for i, t in enumerate(teams)]})
    return bodies


class FakeGitHub:
    """Public GitHub files: guild-keys repo files and gists, keyed by team login (lowercase)."""

    def __init__(self):
        self.repo_files = {}
        self.gists = {}

    def __call__(self, url):
        for team, text in self.repo_files.items():
            if url.lower() == f"https://raw.githubusercontent.com/{team}/guild-keys/main/keys.txt":
                return 200, text
        if url.startswith("https://api.github.com/users/"):
            team = url.split("/users/")[1].split("/")[0].lower()
            if team in self.gists:
                return 200, json.dumps([{"owner": {"login": team}, "files": {"guild-keys.txt": {
                    "raw_url": f"https://gist.githubusercontent.com/{team}/raw/keys.txt"}}}])
            return 200, "[]"
        if url.startswith("https://gist.githubusercontent.com/"):
            team = url.split("githubusercontent.com/")[1].split("/")[0]
            return (200, self.gists[team]) if team in self.gists else (404, "")
        return 404, ""


class FakeHttp:
    def __init__(self, teams):
        self.boards = board_rows(teams)
        self.users = {}       # code -> (user json, emails json, scope)
        self.calls = []
        self.lb_down = False
        self.dns = {}          # host -> list of IPs (DNS over HTTPS answers)
        self.hook_status = {}  # webhook url -> HTTP status to answer

    def add_user(self, code, login, gid, emails, scope="user:email"):
        self.users[code] = ({"login": login, "id": gid}, emails, scope)

    async def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers or {}), body))
        if url.startswith(store.LEADERBOARD_API):
            if self.lb_down:
                return 503, "down"
            tail = url[len(store.LEADERBOARD_API):].lstrip("/") or "overall"
            return 200, self.boards[tail]
        if url.startswith("https://cloudflare-dns.com/dns-query"):
            query = dict(parse_qsl(urlsplit(url).query))
            ips = [ip for ip in self.dns.get(query["name"], []) if (":" in ip) == (query["type"] == "AAAA")]
            return 200, json.dumps({"Answer": [{"type": 28 if ":" in ip else 1, "data": ip} for ip in ips]})
        if url in self.hook_status:
            return self.hook_status[url], "ok"
        if url.startswith("https://api.agentmail.to/"):
            return 200, json.dumps({"message_id": "m1"})
        if url == "https://github.com/login/oauth/access_token":
            form = dict(parse_qsl(body))
            if form.get("code") not in self.users or not form.get("code_verifier"):
                return 200, json.dumps({"error": "bad_verification_code"})
            return 200, json.dumps({"access_token": "gho_TOKEN_" + form["code"], "token_type": "bearer",
                                    "scope": self.users[form["code"]][2]})
        auth = (headers or {}).get("Authorization", "")
        code = auth.replace("Bearer gho_TOKEN_", "")
        if code not in self.users:
            return 401, "{}"
        user, emails, _ = self.users[code]
        if url == "https://api.github.com/user":
            return 200, json.dumps(user)
        if url == "https://api.github.com/user/emails":
            return 200, json.dumps(emails)
        return 404, "{}"


class Board:
    def __init__(self, teams=("TeamA", "TeamB", "TeamC", "Lurrobert"), oauth=True):
        self.db = SqliteDB()
        self.http = FakeHttp(list(teams))
        self.now = T0
        self.config = store.Config(session_secret=SECRET, oauth_client_id="Ov23liq0WDvFZO8QgZ66",
                                   oauth_client_secret="x" * 40 if oauth else "", admin_teams=("lurrobert",))

    def tick(self, seconds=3):
        self.now += seconds

    def request(self, method, path, body=None, headers=None, form=None, cookie=""):
        url = urlsplit(path)
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        raw = ""
        if form is not None:
            raw = urlencode(form)
            hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
        elif body is not None:
            raw = json.dumps(body)
            hdrs.setdefault("content-type", "application/json")
        if cookie:
            hdrs["cookie"] = cookie
        req = app.Req(method=method, path=url.path, query=dict(parse_qsl(url.query)), headers=hdrs, body=raw,
                      scheme="https", host="guild.test")
        ctx = store.Ctx(db=self.db, http=self.http, now=self.now, config=self.config)
        return asyncio.run(app.handle(req, ctx))

    def api(self, method, path, key, body=None):
        self.now += 2  # the burst limit is one write per 2 s per key; tests that probe it call request() directly
        resp = self.request(method, path, body=body, headers={"Authorization": "Bearer " + key} if key else None)
        return resp.status, json.loads(resp.body or "{}")

    # -- humans -------------------------------------------------------------
    def sign_in(self, login, gid, emails=None, scope="user:email"):
        """Runs the whole OAuth web flow. Returns (response, cookie header for the session)."""
        code = f"code{gid}"
        self.http.add_user(code, login, gid, emails if emails is not None else
                           [{"email": f"{login.lower()}@example.com", "verified": True, "primary": True}], scope)
        start = self.request("GET", "/auth/github")
        location = dict(start.headers)["Location"]
        state = dict(parse_qsl(urlsplit(location).query))["state"]
        oauth_cookie = [v for k, v in start.headers if k == "Set-Cookie"][0].split(";")[0]
        resp = self.request("GET", f"/auth/github/callback?code={code}&state={state}", cookie=oauth_cookie)
        session = ""
        for key, value in resp.headers:
            if key == "Set-Cookie" and value.startswith(app.SESSION_COOKIE + "=") and "Max-Age=0" not in value:
                session = value.split(";")[0]
        return resp, session

    def csrf(self, session):
        value = session.split("=", 1)[1]
        nonce = core.read_session(SECRET, value, self.now)["nonce"]
        return core.csrf_token(SECRET, nonce)

    def mint(self, session, agent):
        resp = self.request("POST", "/me/keys", form={"agent": agent, "csrf": self.csrf(session)}, cookie=session)
        assert resp.status == 200, resp.body[:400]
        start = resp.body.index('<code id="fresh-key">') + len('<code id="fresh-key">')
        return resp.body[start:resp.body.index("</code>", start)]

    def team_with_agent(self, login, gid, agent):
        _, session = self.sign_in(login, gid)
        assert session, "sign-in failed"
        return session, self.mint(session, agent)

    def transport(self, key):
        """A client transport that calls the app in-process instead of over HTTP."""
        def send(method, path, body=None):
            return self.api(method, path, key, body)
        return send
