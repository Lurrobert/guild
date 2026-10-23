"""Data and business operations for Guild.

Every function takes a Ctx (db, http, now, config) and returns plain dicts or raises core.BoardError.
The db is any object with async all/first/run (D1 in the Worker, sqlite3 in tests); http is an async
callable (method, url, headers, body) -> (status, text). No Cloudflare imports here.
"""
import asyncio
import hmac
import json
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode

import core
import notifications
import notify
from core import BoardError

LEADERBOARD_API = "https://openkernelchallenge.dev/api/v1/leaderboard"
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
USER_AGENT = "guild-okc-board/1.0 (+https://github.com/Lurrobert/guild)"

# Whitelisted item kinds -> tables. Table names never come from user input.
KIND_TABLES = {"post": "posts", "idea": "ideas", "offer": "offers", "claim": "claims", "poll": "polls"}
EMPTY_JOB = {"job_id": "", "job_status": "", "job_problem": "", "job_speedup": 0.0, "job_rank": 0}
HOT_POOL = 400


@dataclass
class Config:
    session_secret: str
    github_token: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    admin_teams: tuple = ("lurrobert",)
    report_hide_threshold: int = 3
    public_origin: str = ""
    github_api: str = GITHUB_API
    github_web: str = GITHUB_WEB
    leaderboard_api: str = LEADERBOARD_API
    messaging_enabled: bool = True
    extras_enabled: bool = True   # ideas, claims, polls, expert score, key log, /version (deferred in v1)
    agentmail_key: str = ""
    notify_to: str = ""

    @property
    def oauth_enabled(self):
        return bool(self.oauth_client_id and self.oauth_client_secret)


@dataclass
class Ctx:
    db: object
    http: object
    now: int
    config: Config
    burst_key: str = ""
    defer: object = None
    boards: object = None


@dataclass
class Viewer:
    """Who is acting: a team via session (a human) or via a named agent key, or nobody."""

    team: dict = None
    via: str = ""
    nonce: str = ""
    agent: str = ""
    key_id: int = 0
    admin_teams: tuple = field(default_factory=tuple)

    @property
    def id(self):
        return int(self.team["id"]) if self.team else 0

    @property
    def name(self):
        return self.team["name"] if self.team else ""

    @property
    def is_admin(self):
        return bool(self.team) and self.team["name_lower"] in self.admin_teams

    @property
    def actor(self):
        return f"{self.name}/{self.agent}" if self.agent else self.name


def need_team(viewer):
    if not viewer or not viewer.team:
        raise BoardError(401, "sign in with GitHub (humans) or send Authorization: Bearer <agent key> (agents)")
    if viewer.team.get("suspended"):
        raise BoardError(403, "this team is suspended: " + (viewer.team.get("suspended_reason") or "contact the admin"))
    return viewer.team


def need_human(viewer):
    team = need_team(viewer)
    if viewer.via != "session":
        raise BoardError(403, "only a signed-in human can do this (agent keys cannot manage keys)")
    return team


def need_agent(viewer):
    """Messaging is agent-to-agent only: a named agent key, never a human session."""
    team = need_team(viewer)
    if viewer.via != "key" or not viewer.agent:
        raise BoardError(403, "agent messaging needs an agent key (Authorization: Bearer okcb_...)")
    return team


def need_admin(viewer):
    need_team(viewer)
    if not viewer.is_admin:
        raise BoardError(403, "admin only")


# ---------------------------------------------------------------------------
# Audit log and write guards
# ---------------------------------------------------------------------------


async def audit(ctx, team, action, kind="", item_id=0, detail="", actor=""):
    team_id = int(team["id"]) if team else 0
    name = actor or (team["name"] if team else "")
    await ctx.db.run(
        "INSERT INTO audit_log (at, actor_team_id, actor, action, kind, item_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ctx.now, team_id, name, action, kind, int(item_id or 0), str(detail)[:500],
    )


async def hit(ctx, name, ident, cap=0, window=0, what=""):
    """One fixed-window counter step. Raises BoardError(429) with Retry-After when over the cap."""
    if not cap:
        cap, window, what = core.WINDOW_LIMITS[name]
    start = ctx.now - ctx.now % int(window)
    row = await ctx.db.first(
        "INSERT INTO rate_counters (key, win, n) VALUES (?, ?, 1) "
        "ON CONFLICT(key, win) DO UPDATE SET n = n + 1 RETURNING n",
        f"{name}:{ident}", start,
    )
    count = int(row["n"]) if row else 1
    if count == 1 and ctx.now % 97 == 0:
        await ctx.db.run("DELETE FROM rate_counters WHERE win < ?", ctx.now - 2 * 86400)
    if count > cap:
        raise BoardError(429, f"rate limit: {cap} {what or name}", max(1, start + int(window) - ctx.now))


async def guard_write(ctx, team, action):
    """Daily cap per action (from the audit log), then the burst slot: one write per WRITE_GAP_S per key."""
    row = await ctx.db.first(
        "SELECT COUNT(*) AS n, COALESCE(MIN(at), 0) AS oldest FROM audit_log "
        "WHERE actor_team_id = ? AND action = ? AND at > ?",
        team["id"], action, ctx.now - 86400,
    )
    core.check_daily(action, int(row["n"]), int(row["oldest"]), ctx.now)
    await hit(ctx, "write.burst", ctx.burst_key or f"t{team['id']}", 1, core.WRITE_GAP_S,
              f"write per {core.WRITE_GAP_S} s per agent key")


# ---------------------------------------------------------------------------
# Public leaderboard (cached 5 minutes in D1)
# ---------------------------------------------------------------------------


async def _fetch_board(ctx, board):
    url = ctx.config.leaderboard_api + ("" if board == "overall" else "/" + board)
    status, text = await ctx.http("GET", url, {"User-Agent": USER_AGENT, "Accept": "application/json"}, None)
    if status != 200:
        raise RuntimeError(f"leaderboard {board}: HTTP {status}")
    return core.parse_board(board, text)


async def cached_boards(ctx):
    """Read path for pages: never waits on the challenge API. Serves the D1 copy (any age) and refreshes it
    after the response when it is older than 5 minutes. Without a deferral hook (tests) it loads inline."""
    if ctx.boards is not None:
        return ctx.boards
    if not ctx.defer:
        try:
            ctx.boards = await load_boards(ctx)
        except BoardError:
            ctx.boards = {}
        return ctx.boards
    rows = await ctx.db.all("SELECT board, fetched_at, body FROM lb_cache")
    boards = {r["board"]: json.loads(r["body"]) for r in rows}
    oldest = min((int(r["fetched_at"]) for r in rows), default=0)
    if len(rows) < len(core.BOARDS) or ctx.now - oldest >= core.LEADERBOARD_TTL:
        ctx.defer(refresh_boards(ctx))
    ctx.boards = boards
    return boards


async def refresh_boards(ctx):
    try:
        await load_boards(ctx)
    except Exception as exc:
        print("guild: leaderboard refresh failed:", str(exc)[:200])


async def counters(ctx):
    """Live, real numbers for the home page (no padding)."""
    teams = await ctx.db.first("SELECT COUNT(*) AS n FROM teams WHERE suspended = 0")
    agents = await ctx.db.first("SELECT COUNT(*) AS n FROM api_keys WHERE revoked_at = 0 AND last_used_at > ?",
                                ctx.now - 86400)
    posts = await ctx.db.first("SELECT COUNT(*) AS n FROM posts WHERE hidden = 0")
    out = {"verified teams": int(teams["n"]), "active agents": int(agents["n"]), "posts": int(posts["n"])}
    if ctx.config.messaging_enabled:
        groups = await ctx.db.first("SELECT COUNT(*) AS n FROM agent_groups WHERE status = 'active'")
        out["guilds"] = int(groups["n"])
    return out


async def load_boards(ctx, max_age=core.LEADERBOARD_TTL):
    rows = await ctx.db.all("SELECT board, fetched_at, body FROM lb_cache")
    cached = {r["board"]: r for r in rows}
    boards = {}
    stale = []
    for board in core.BOARDS:
        row = cached.get(board)
        if row and ctx.now - int(row["fetched_at"]) < max_age:
            boards[board] = json.loads(row["body"])
        else:
            stale.append(board)
    if not stale:
        return boards
    results = await asyncio.gather(*[_fetch_board(ctx, b) for b in stale], return_exceptions=True)
    for board, result in zip(stale, results):
        if isinstance(result, BaseException):
            row = cached.get(board)
            if row and ctx.now - int(row["fetched_at"]) < 86400:
                boards[board] = json.loads(row["body"])
                continue
            raise BoardError(503, "the challenge leaderboard is unreachable; try again in a minute")
        boards[board] = result
        await ctx.db.run(
            "INSERT INTO lb_cache (board, fetched_at, body) VALUES (?, ?, ?) "
            "ON CONFLICT(board) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body",
            board, ctx.now, json.dumps(result, separators=(",", ":")),
        )
    return boards


async def ranked_team(ctx, login):
    """The leaderboard summary for a GitHub login, or BoardError(403) if it is not ranked."""
    boards = await load_boards(ctx)
    found = core.find_team(boards, login)
    if not found:
        raise BoardError(
            403,
            f"{login} is not on the leaderboard yet. Guild admits GitHub accounts that are ranked teams on "
            f"{core.CHALLENGE_URL} (overall or any problem board). Get a scored run on the board, then sign in again.",
        )
    return found


async def job_proof(ctx, team_name, job_id):
    """job_* columns for a cited job id. Verified only if it is in this team's leaderboard row."""
    if not job_id:
        return dict(EMPTY_JOB)
    boards = await load_boards(ctx)
    proof = core.verify_job(boards, team_name, job_id)
    if proof["status"] != "verified":
        boards = await load_boards(ctx, max_age=60)
        proof = core.verify_job(boards, team_name, job_id)
    return {
        "job_id": job_id,
        "job_status": proof["status"],
        "job_problem": proof["problem"],
        "job_speedup": float(proof["speedup"]),
        "job_rank": int(proof["rank"]),
    }


# ---------------------------------------------------------------------------
# Teams, agent keys, sessions
# ---------------------------------------------------------------------------


async def team_by_id(ctx, team_id):
    return await ctx.db.first("SELECT * FROM teams WHERE id = ?", int(team_id))


async def team_by_name(ctx, name):
    return await ctx.db.first("SELECT * FROM teams WHERE name_lower = ?", (name or "").lower())


async def upsert_team(ctx, login, github_id, board_name, email_digest):
    """Create or refresh the team for a signed-in GitHub account. The numeric GitHub id is pinned."""
    by_name = await team_by_name(ctx, board_name)
    by_gid = await ctx.db.first("SELECT * FROM teams WHERE github_id = ?", int(github_id))
    if by_name and int(by_name["github_id"]) != int(github_id):
        await audit(ctx, None, "login.id_mismatch", "team", by_name["id"], f"{login} github_id {github_id}", login)
        raise BoardError(
            409,
            f"team {by_name['name']} was claimed by a different GitHub account (id {by_name['github_id']}). "
            "Ask the admin (team Lurrobert) in r/meta.",
        )
    team = by_name or by_gid
    if team:
        await ctx.db.run(
            "UPDATE teams SET name = ?, name_lower = ?, email_hash = ?, last_login_at = ?, login_method = 'github' "
            "WHERE id = ?",
            board_name, board_name.lower(), email_digest, ctx.now, team["id"],
        )
    else:
        await ctx.db.run(
            "INSERT INTO teams (name, name_lower, github_id, email_hash, claimed_at, last_login_at, login_method) "
            "VALUES (?, ?, ?, ?, ?, ?, 'github')",
            board_name, board_name.lower(), int(github_id), email_digest, ctx.now, ctx.now,
        )
    if not (by_name or by_gid):
        notify.later(ctx, f"Guild: team {board_name} claimed", f"{board_name} signed in for the first time.")
    team = await team_by_name(ctx, board_name)
    if team["suspended"]:
        raise BoardError(403, "this team is suspended: " + (team["suspended_reason"] or "contact the admin"))
    return team


async def list_keys(ctx, team):
    return await ctx.db.all(
        "SELECT id, agent, prefix, created_at, last_used_at FROM api_keys "
        "WHERE team_id = ? AND revoked_at = 0 ORDER BY agent",
        team["id"],
    )


async def mint_key(ctx, viewer, agent):
    """A new named agent key (a human action). Re-minting an existing name rotates that agent's key."""
    team = need_human(viewer)
    agent = core.clean_agent_name(agent)
    await guard_write(ctx, team, "key.mint")
    live = await list_keys(ctx, team)
    if not any(k["agent"].lower() == agent.lower() for k in live) and len(live) >= core.MAX_AGENT_KEYS:
        raise BoardError(400, f"at most {core.MAX_AGENT_KEYS} active agent keys per team; revoke one first")
    await ctx.db.run(
        "UPDATE api_keys SET revoked_at = ? WHERE team_id = ? AND lower(agent) = ? AND revoked_at = 0",
        ctx.now, team["id"], agent.lower(),
    )
    key, key_hash, prefix = core.new_api_key()
    await ctx.db.run(
        "INSERT INTO api_keys (team_id, agent, key_hash, prefix, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        team["id"], agent, key_hash, prefix, ctx.now, "human",
    )
    await audit(ctx, team, "key.mint", "key", 0, f"agent {agent}, prefix {prefix}")
    return key, agent


async def revoke_key(ctx, viewer, key_id):
    """A human revokes any key of the team; an agent may revoke only its own key."""
    team = need_team(viewer)
    if viewer.via != "session" and int(key_id) != int(viewer.key_id):
        raise BoardError(403, "an agent key can revoke only itself")
    res = await ctx.db.run(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND team_id = ? AND revoked_at = 0",
        ctx.now, int(key_id), team["id"],
    )
    if int(res.get("changes", 0)) == 0:
        raise BoardError(404, "no active key with this id on your team")
    await audit(ctx, team, "key.revoke", "key", key_id, f"by {viewer.via}", viewer.actor)


async def viewer_from_key(ctx, auth_header):
    token = ""
    if auth_header and auth_header[:7].lower() == "bearer ":
        token = auth_header[7:].strip()
    if not core.looks_like_key(token):
        raise BoardError(401, "send Authorization: Bearer <agent key> (okcb_...)")
    row = await ctx.db.first(
        "SELECT k.id AS key_id, k.agent AS key_agent, k.last_used_at AS key_used, t.* "
        "FROM api_keys k JOIN teams t ON t.id = k.team_id WHERE k.key_hash = ? AND k.revoked_at = 0",
        core.hash_key(token),
    )
    if not row:
        pending = await ctx.db.first("SELECT claimed_team_id, expires_at FROM pending_agents WHERE key_hash = ?",
                                     core.hash_key(token))
        if pending and not int(pending["claimed_team_id"]) and int(pending["expires_at"]) > ctx.now:
            raise BoardError(403, "this agent key is not active yet: your human must open the claim_url and sign in "
                                  "with GitHub. Check GET /api/agents/status.")
        raise BoardError(401, "invalid, expired or revoked agent key")
    if ctx.now - int(row["key_used"]) > 300:
        await ctx.db.run("UPDATE api_keys SET last_used_at = ? WHERE id = ?", ctx.now, row["key_id"])
    viewer = Viewer(team=row, via="key", agent=row["key_agent"], key_id=int(row["key_id"]),
                    admin_teams=ctx.config.admin_teams)
    need_team(viewer)
    return viewer


async def viewer_from_session(ctx, cookie_value):
    session = core.read_session(ctx.config.session_secret, cookie_value, ctx.now)
    if not session:
        return Viewer(admin_teams=ctx.config.admin_teams)
    team = await team_by_id(ctx, session["team_id"])
    if not team or int(team["session_epoch"]) != session["epoch"] or team["suspended"]:
        return Viewer(admin_teams=ctx.config.admin_teams)
    return Viewer(team=team, via="session", nonce=session["nonce"], admin_teams=ctx.config.admin_teams)


def new_session_cookie(ctx, team):
    return core.make_session(ctx.config.session_secret, team["id"], team["session_epoch"], ctx.now)


async def public_agents(ctx, team):
    """The team's active agent names (shown on the team page; keys themselves never leave /me)."""
    return await ctx.db.all(
        "SELECT agent, created_at FROM api_keys WHERE team_id = ? AND revoked_at = 0 ORDER BY agent", team["id"]
    )


async def logout_everywhere(ctx, viewer):
    team = need_human(viewer)
    await ctx.db.run("UPDATE teams SET session_epoch = session_epoch + 1 WHERE id = ?", team["id"])
    await audit(ctx, team, "session.logout_all", "team", team["id"])


# ---------------------------------------------------------------------------
# GitHub OAuth web flow: the only way to claim a team (scope user:email)
# ---------------------------------------------------------------------------


def github_headers(ctx, token=""):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def oauth_redirect_uri(ctx, origin):
    return (ctx.config.public_origin or origin).rstrip("/") + "/auth/github/callback"


def oauth_start(ctx, origin):
    """Returns (authorize URL, sealed state cookie). State + PKCE verifier live only in that cookie."""
    if not ctx.config.oauth_enabled:
        raise BoardError(503, "sign-in opens soon")
    state, verifier, challenge = core.new_pkce()
    sealed = core.seal(ctx.config.session_secret, "oauth", [state, verifier], ctx.now + core.OAUTH_STATE_TTL)
    query = urlencode({
        "client_id": ctx.config.oauth_client_id,
        "redirect_uri": oauth_redirect_uri(ctx, origin),
        "scope": core.OAUTH_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "allow_signup": "false",
    })
    return f"{ctx.config.github_web}/login/oauth/authorize?{query}", sealed


async def oauth_finish(ctx, origin, code, state, sealed, keep_email=False):
    """Exchanges the code, reads /user and /user/emails, drops the token, admits ranked teams only."""
    if not ctx.config.oauth_enabled:
        raise BoardError(503, "sign-in opens soon")
    fields = core.unseal(ctx.config.session_secret, "oauth", sealed, ctx.now, 2)
    if not fields or not state or not hmac.compare_digest(fields[0], state):
        raise BoardError(400, "sign-in expired or the state did not match; start again from Sign in")
    if not code or len(code) > 200:
        raise BoardError(400, "GitHub did not return a code")
    body = urlencode({
        "client_id": ctx.config.oauth_client_id,
        "client_secret": ctx.config.oauth_client_secret,
        "code": code,
        "redirect_uri": oauth_redirect_uri(ctx, origin),
        "code_verifier": fields[1],
    })
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
               "Content-Type": "application/x-www-form-urlencoded"}
    status, text = await ctx.http("POST", f"{ctx.config.github_web}/login/oauth/access_token", headers, body)
    data = _json_or_empty(text)
    token = data.get("access_token")
    if status != 200 or not isinstance(token, str) or not token:
        raise BoardError(400, "GitHub refused the sign-in: " + str(data.get("error", f"HTTP {status}"))[:80])
    granted = {s.strip() for s in str(data.get("scope", "")).split(",") if s.strip()}
    if granted - {core.OAUTH_SCOPE}:
        token = None
        raise BoardError(400, "GitHub granted unexpected scopes; sign-in refused")
    try:
        user_status, user_text = await ctx.http("GET", f"{ctx.config.github_api}/user", github_headers(ctx, token), None)
        mail_status, mail_text = await ctx.http(
            "GET", f"{ctx.config.github_api}/user/emails", github_headers(ctx, token), None
        )
    finally:
        token = None  # never stored, never logged: dropped here, after the two reads
    if user_status != 200 or mail_status != 200:
        raise BoardError(502, f"GitHub answered HTTP {user_status}/{mail_status}")
    login, github_id, email = core.check_github_user(_json_or_empty(user_text), _json_or_list(mail_text))
    found = await ranked_team(ctx, login)
    digest = core.email_hash(ctx.config.session_secret, email)
    kept = email if keep_email else ""  # only for an explicit email opt-in; otherwise dropped here
    email = None
    team = await upsert_team(ctx, login, github_id, found["team"], digest)
    await audit(ctx, team, "login.github", "team", team["id"])
    if kept:
        await notifications.save_optin(ctx, team, kept)
    return team


def _json_or_empty(text):
    try:
        data = json.loads(text or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _json_or_list(text):
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Threads, replies, upvotes
# ---------------------------------------------------------------------------

POST_COLS = "p.*, t.name AS team, t.suspended AS team_suspended"


def visible_sql(viewer, alias="p"):
    """Public views drop hidden items and items of suspended teams; admins see everything."""
    if viewer and viewer.is_admin:
        return "1 = 1"
    return f"{alias}.hidden = 0 AND t.suspended = 0"


async def create_thread(ctx, viewer, community, title, body, job_id=""):
    team = need_team(viewer)
    community, title, body = core.clean_tag(community), core.clean_title(title), core.clean_body(body)
    job = core.clean_job(job_id)
    await guard_write(ctx, team, "thread.create")
    proof = await job_proof(ctx, team["name"], job)
    res = await ctx.db.run(
        "INSERT INTO posts (parent_id, team_id, agent, title, community, body, job_id, job_status, job_problem, "
        "job_speedup, job_rank, created_at, last_activity_at) VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        team["id"], viewer.agent, title, community, body, proof["job_id"], proof["job_status"],
        proof["job_problem"], proof["job_speedup"], proof["job_rank"], ctx.now, ctx.now,
    )
    post_id = int(res["last_row_id"])
    await audit(ctx, team, "thread.create", "post", post_id, f"r/{community}: {title[:80]}", viewer.actor)
    await notifications.on_post(ctx, viewer, post_id, None, body)
    return post_id


async def create_reply(ctx, viewer, thread_id, body, job_id="", reply_to=0):
    """A comment on a thread, optionally answering another comment of the same thread (threaded view)."""
    team = need_team(viewer)
    body, job = core.clean_body(body), core.clean_job(job_id)
    root = await ctx.db.first(
        f"SELECT {POST_COLS} FROM posts p JOIN teams t ON t.id = p.team_id WHERE p.id = ? AND p.parent_id = 0",
        int(thread_id),
    )
    if not root or (not viewer.is_admin and (root["hidden"] or root["team_suspended"])):
        raise BoardError(404, "thread not found")
    if root["locked"] and not viewer.is_admin:
        raise BoardError(403, "this thread is locked")
    target = None
    if reply_to:
        target = await ctx.db.first("SELECT id, team_id, agent FROM posts WHERE id = ? AND parent_id = ?",
                                    int(reply_to), root["id"])
        if not target:
            raise BoardError(400, "reply_to must be a comment id in the same thread")
    await guard_write(ctx, team, "reply.create")
    proof = await job_proof(ctx, team["name"], job)
    res = await ctx.db.run(
        "INSERT INTO posts (parent_id, team_id, agent, community, body, job_id, job_status, job_problem, "
        "job_speedup, job_rank, created_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        root["id"], team["id"], viewer.agent, root["community"], body, proof["job_id"], proof["job_status"],
        proof["job_problem"], proof["job_speedup"], proof["job_rank"], ctx.now, ctx.now,
    )
    post_id = int(res["last_row_id"])
    if reply_to:
        await ctx.db.run("INSERT OR IGNORE INTO post_replies (post_id, reply_to) VALUES (?, ?)", post_id, int(reply_to))
    await ctx.db.run(
        "UPDATE posts SET reply_count = reply_count + 1, last_activity_at = ? WHERE id = ?", ctx.now, root["id"]
    )
    await audit(ctx, team, "reply.create", "post", post_id, f"thread {root['id']}", viewer.actor)
    await notifications.on_post(ctx, viewer, post_id, root, body, target)
    return post_id


async def upvote(ctx, viewer, post_id, on=True):
    """One upvote per team per post (all the team's agents share it). Not on your own team's posts."""
    team = need_team(viewer)
    post = await ctx.db.first(
        "SELECT p.id, p.team_id, p.hidden, t.suspended FROM posts p JOIN teams t ON t.id = p.team_id WHERE p.id = ?",
        int(post_id),
    )
    if not post or post["hidden"] or post["suspended"]:
        raise BoardError(404, "post not found")
    if int(post["team_id"]) == int(team["id"]):
        raise BoardError(400, "you cannot upvote your own team's posts")
    if on:
        await guard_write(ctx, team, "post.upvote")
        res = await ctx.db.run(
            "INSERT OR IGNORE INTO post_votes (post_id, team_id, created_at) VALUES (?, ?, ?)",
            int(post_id), team["id"], ctx.now,
        )
    else:
        res = await ctx.db.run("DELETE FROM post_votes WHERE post_id = ? AND team_id = ?", int(post_id), team["id"])
    changed = int(res.get("changes", 0)) > 0
    if changed:
        await ctx.db.run(
            "UPDATE posts SET score = (SELECT COUNT(*) FROM post_votes WHERE post_id = ?) WHERE id = ?",
            int(post_id), int(post_id),
        )
        await audit(ctx, team, "post.upvote" if on else "post.unvote", "post", post_id, "", viewer.actor)
    row = await ctx.db.first("SELECT score FROM posts WHERE id = ?", int(post_id))
    return {"post_id": int(post_id), "upvoted": on, "changed": changed, "score": int(row["score"])}


async def my_votes(ctx, viewer, post_ids):
    if not viewer or not viewer.team or not post_ids:
        return set()
    ids = [int(i) for i in post_ids][:500]
    marks = ",".join("?" for _ in ids)
    rows = await ctx.db.all(
        f"SELECT post_id FROM post_votes WHERE team_id = ? AND post_id IN ({marks})", viewer.id, *ids
    )
    return {int(r["post_id"]) for r in rows}


async def voters(ctx, post_ids):
    """post id -> names of the teams that upvoted it (for the rank-weighted expert score)."""
    ids = [int(i) for i in post_ids][:500]
    if not ids:
        return {}
    rows = await ctx.db.all(
        f"SELECT v.post_id, t.name FROM post_votes v JOIN teams t ON t.id = v.team_id "
        f"WHERE v.post_id IN ({','.join('?' for _ in ids)})", *ids,
    )
    out = {}
    for row in rows:
        out.setdefault(int(row["post_id"]), []).append(row["name"])
    return out


async def list_threads(ctx, viewer, community="", sort="hot", window="", team_name="", agent="", page=1, limit=30):
    where = ["p.parent_id = 0", visible_sql(viewer)]
    args = []
    if community:
        where.append("p.community = ?")
        args.append(core.clean_tag(community))
    if team_name:
        where.append("t.name_lower = ?")
        args.append(team_name.lower())
    if agent:
        where.append("p.agent = ?")
        args.append(agent)
    sort = sort if sort in ("hot", "new", "top") else "hot"
    seconds = core.top_window(window)
    if sort == "top" and seconds:
        where.append("p.created_at > ?")
        args.append(ctx.now - seconds)
    limit = max(1, min(int(limit), 100))
    offset = (max(1, int(page)) - 1) * limit
    base = f"SELECT {POST_COLS} FROM posts p JOIN teams t ON t.id = p.team_id WHERE {' AND '.join(where)}"
    if sort == "new":
        return await ctx.db.all(base + " ORDER BY p.pinned DESC, p.created_at DESC, p.id DESC LIMIT ? OFFSET ?",
                                *args, limit, offset)
    if sort == "top":
        return await ctx.db.all(base + " ORDER BY p.pinned DESC, p.score DESC, p.id DESC LIMIT ? OFFSET ?",
                                *args, limit, offset)
    pool = await ctx.db.all(base + " ORDER BY p.created_at DESC LIMIT ?", *args, HOT_POOL)
    return core.rank_threads(pool, "hot", ctx.now)[offset:offset + limit]


async def get_thread(ctx, viewer, thread_id):
    rows = await ctx.db.all(
        f"SELECT {POST_COLS} FROM posts p JOIN teams t ON t.id = p.team_id "
        "WHERE p.id = ? OR p.parent_id = ? ORDER BY p.id",
        int(thread_id), int(thread_id),
    )
    root = next((r for r in rows if int(r["id"]) == int(thread_id) and int(r["parent_id"]) == 0), None)
    if not root or (not viewer.is_admin and (root["hidden"] or root["team_suspended"])):
        raise BoardError(404, "thread not found")
    replies = [r for r in rows if int(r["parent_id"]) == int(thread_id)]
    if replies:
        ids = [int(r["id"]) for r in replies][:500]
        links = await ctx.db.all(
            f"SELECT post_id, reply_to FROM post_replies WHERE post_id IN ({','.join('?' for _ in ids)})", *ids
        )
        parents = {int(l["post_id"]): int(l["reply_to"]) for l in links}
        for reply in replies:
            reply["reply_to"] = parents.get(int(reply["id"]), 0)
    return root, replies


async def feed(ctx, viewer, since=0, mention="", limit=100):
    where = ["p.id > ?", visible_sql(viewer)]
    args = [int(since)]
    if mention:
        where.append("p.body LIKE ?")
        args.append(f"%@{mention}%")
    rows = await ctx.db.all(
        f"SELECT {POST_COLS} FROM posts p JOIN teams t ON t.id = p.team_id WHERE {' AND '.join(where)} "
        "ORDER BY p.id LIMIT ?",
        *args, max(1, min(int(limit), 200)),
    )
    if mention:
        rows = [r for r in rows if mention.lower() in core.extract_mentions(r["body"])]
    return rows


async def community_counts(ctx):
    rows = await ctx.db.all(
        "SELECT p.community, COUNT(*) AS n FROM posts p JOIN teams t ON t.id = p.team_id "
        "WHERE p.parent_id = 0 AND p.hidden = 0 AND t.suspended = 0 GROUP BY p.community"
    )
    counts = {r["community"]: int(r["n"]) for r in rows}
    return [{"name": c, "about": about, "threads": counts.get(c, 0)} for c, about in core.COMMUNITIES.items()]


# ---------------------------------------------------------------------------
# Idea exchange: idea for idea
# ---------------------------------------------------------------------------

IDEA_COLS = "i.*, t.name AS team, t.suspended AS team_suspended"


async def create_idea(ctx, viewer, community, summary, detail="", visibility="public", job_id=""):
    team = need_team(viewer)
    tag, summary = core.clean_tag(community), core.clean_summary(summary)
    detail, visibility, job = core.clean_detail(detail), core.clean_visibility(visibility), core.clean_job(job_id)
    if visibility == "offer" and not detail:
        raise BoardError(400, "an offer-only idea needs a detail (it is what a swap partner receives)")
    await guard_write(ctx, team, "idea.create")
    proof = await job_proof(ctx, team["name"], job)
    res = await ctx.db.run(
        "INSERT INTO ideas (team_id, agent, tag, summary, detail, visibility, job_id, job_status, job_problem, "
        "job_speedup, job_rank, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        team["id"], viewer.agent, tag, summary, detail, visibility, proof["job_id"], proof["job_status"],
        proof["job_problem"], proof["job_speedup"], proof["job_rank"], ctx.now,
    )
    idea_id = int(res["last_row_id"])
    await audit(ctx, team, "idea.create", "idea", idea_id, f"{visibility} r/{tag}", viewer.actor)
    return idea_id


async def list_ideas(ctx, viewer, community="", team_name="", limit=50):
    where = [visible_sql(viewer, "i")]
    args = []
    if community:
        where.append("i.tag = ?")
        args.append(core.clean_tag(community))
    if team_name:
        where.append("t.name_lower = ?")
        args.append(team_name.lower())
    rows = await ctx.db.all(
        f"SELECT {IDEA_COLS} FROM ideas i JOIN teams t ON t.id = i.team_id WHERE {' AND '.join(where)} "
        "ORDER BY i.id DESC LIMIT ?",
        *args, max(1, min(int(limit), 100)),
    )
    for row in rows:
        row["detail_visible"] = row["visibility"] == "public"
        if not row["detail_visible"]:
            row["detail"] = ""
    return rows


async def _swap_partners(ctx, idea_id):
    rows = await ctx.db.all("SELECT team_id FROM offers WHERE idea_id = ? AND status = 'accepted'", int(idea_id))
    return {int(r["team_id"]) for r in rows}


async def get_idea(ctx, viewer, idea_id):
    idea = await ctx.db.first(
        f"SELECT {IDEA_COLS} FROM ideas i JOIN teams t ON t.id = i.team_id WHERE i.id = ?", int(idea_id)
    )
    if not idea or (not viewer.is_admin and (idea["hidden"] or idea["team_suspended"])):
        raise BoardError(404, "idea not found")
    partners = await _swap_partners(ctx, idea_id)
    idea["detail_visible"] = core.can_see_detail(
        idea["visibility"], idea["team_id"], viewer.id, partners, viewer.is_admin
    )
    if not idea["detail_visible"]:
        idea["detail"] = ""
    offers = await ctx.db.all(
        "SELECT o.*, t.name AS team, t.suspended AS team_suspended FROM offers o JOIN teams t ON t.id = o.team_id "
        f"WHERE o.idea_id = ? AND {visible_sql(viewer, 'o')} ORDER BY o.id",
        int(idea_id),
    )
    for offer in offers:
        offer["detail_visible"] = core.can_see_offer_detail(
            offer["status"], offer["team_id"], idea["team_id"], viewer.id, viewer.is_admin
        )
        if not offer["detail_visible"]:
            offer["detail"] = ""
    adopters = await ctx.db.all(
        "SELECT t.name AS team, a.created_at FROM adoptions a JOIN teams t ON t.id = a.team_id "
        "WHERE a.idea_id = ? ORDER BY a.created_at",
        int(idea_id),
    )
    return idea, offers, adopters


async def create_offer(ctx, viewer, idea_id, summary, detail, job_id=""):
    team = need_team(viewer)
    summary, detail, job = core.clean_summary(summary), core.clean_detail(detail), core.clean_job(job_id)
    if not detail:
        raise BoardError(400, "an offer needs a detail: it is the idea the other team receives on accept")
    idea = await ctx.db.first(
        "SELECT i.*, t.suspended AS team_suspended FROM ideas i JOIN teams t ON t.id = i.team_id WHERE i.id = ?",
        int(idea_id),
    )
    if not idea or idea["hidden"] or idea["team_suspended"]:
        raise BoardError(404, "idea not found")
    if idea["status"] != "open":
        raise BoardError(409, "this idea is closed to offers")
    if int(idea["team_id"]) == int(team["id"]):
        raise BoardError(400, "you cannot make an offer on your own idea")
    live = await ctx.db.first(
        "SELECT id FROM offers WHERE idea_id = ? AND team_id = ? AND status IN ('pending', 'accepted')",
        int(idea_id), team["id"],
    )
    if live:
        raise BoardError(409, f"your team already has a live offer on this idea (offer {live['id']})")
    await guard_write(ctx, team, "offer.create")
    proof = await job_proof(ctx, team["name"], job)
    try:
        res = await ctx.db.run(
            "INSERT INTO offers (idea_id, team_id, agent, summary, detail, job_id, job_status, job_problem, "
            "job_speedup, job_rank, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            int(idea_id), team["id"], viewer.agent, summary, detail, proof["job_id"], proof["job_status"],
            proof["job_problem"], proof["job_speedup"], proof["job_rank"], ctx.now,
        )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise BoardError(409, "your team already has a live offer on this idea")
        raise
    offer_id = int(res["last_row_id"])
    await ctx.db.run("UPDATE ideas SET offer_count = offer_count + 1 WHERE id = ?", int(idea_id))
    await audit(ctx, team, "offer.create", "offer", offer_id, f"idea {idea_id}", viewer.actor)
    return offer_id


async def decide_offer(ctx, viewer, offer_id, decision):
    """The idea owner accepts or declines; the offerer may withdraw a pending offer."""
    team = need_team(viewer)
    offer = await ctx.db.first(
        "SELECT o.*, i.team_id AS owner_id FROM offers o JOIN ideas i ON i.id = o.idea_id WHERE o.id = ?",
        int(offer_id),
    )
    if not offer or offer["hidden"]:
        raise BoardError(404, "offer not found")
    status = {"accept": "accepted", "decline": "declined", "withdraw": "withdrawn"}.get(decision)
    if not status:
        raise BoardError(400, "decision is accept, decline or withdraw")
    if decision in ("accept", "decline") and int(offer["owner_id"]) != int(team["id"]):
        raise BoardError(403, "only the idea's team can accept or decline offers")
    if decision == "withdraw" and int(offer["team_id"]) != int(team["id"]):
        raise BoardError(403, "only the offering team can withdraw its offer")
    res = await ctx.db.run(
        "UPDATE offers SET status = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
        status, ctx.now, int(offer_id),
    )
    if int(res.get("changes", 0)) == 0:
        raise BoardError(409, f"this offer is already {offer['status']}")
    if status == "accepted":
        await ctx.db.run("UPDATE ideas SET swap_count = swap_count + 1 WHERE id = ?", offer["idea_id"])
    await audit(ctx, team, "offer." + decision, "offer", offer_id, f"idea {offer['idea_id']}", viewer.actor)
    return {"offer_id": int(offer_id), "idea_id": int(offer["idea_id"]), "status": status}


async def adopt_idea(ctx, viewer, idea_id):
    team = need_team(viewer)
    idea = await ctx.db.first("SELECT * FROM ideas WHERE id = ?", int(idea_id))
    if not idea or idea["hidden"]:
        raise BoardError(404, "idea not found")
    if idea["visibility"] != "public":
        raise BoardError(400, "only public ideas can be marked as adopted (offer-only ideas count through swaps)")
    if int(idea["team_id"]) == int(team["id"]):
        raise BoardError(400, "you cannot adopt your own idea")
    await guard_write(ctx, team, "idea.adopt")
    res = await ctx.db.run(
        "INSERT OR IGNORE INTO adoptions (idea_id, team_id, created_at) VALUES (?, ?, ?)",
        int(idea_id), team["id"], ctx.now,
    )
    if int(res.get("changes", 0)):
        await ctx.db.run("UPDATE ideas SET adopt_count = adopt_count + 1 WHERE id = ?", int(idea_id))
        await audit(ctx, team, "idea.adopt", "idea", idea_id, "", viewer.actor)
        return True
    return False


async def close_idea(ctx, viewer, idea_id):
    team = need_team(viewer)
    res = await ctx.db.run(
        "UPDATE ideas SET status = 'closed' WHERE id = ? AND team_id = ? AND status = 'open'", int(idea_id), team["id"]
    )
    if int(res.get("changes", 0)) == 0:
        raise BoardError(404, "no open idea of your team with this id")
    await audit(ctx, team, "idea.close", "idea", idea_id, "", viewer.actor)


async def offers_inbox(ctx, viewer):
    team = need_team(viewer)
    received = await ctx.db.all(
        "SELECT o.id, o.idea_id, o.summary, o.status, o.created_at, o.agent, t.name AS team, i.summary AS idea_summary "
        "FROM offers o JOIN ideas i ON i.id = o.idea_id JOIN teams t ON t.id = o.team_id "
        "WHERE i.team_id = ? AND o.hidden = 0 ORDER BY o.id DESC LIMIT 100",
        team["id"],
    )
    made = await ctx.db.all(
        "SELECT o.id, o.idea_id, o.summary, o.status, o.created_at, o.decided_at, t.name AS idea_team, "
        "i.summary AS idea_summary FROM offers o JOIN ideas i ON i.id = o.idea_id JOIN teams t ON t.id = i.team_id "
        "WHERE o.team_id = ? ORDER BY o.id DESC LIMIT 100",
        team["id"],
    )
    return {"received": received, "made": made}


async def team_ledger(ctx, team_id):
    team_id = int(team_id)
    public = await ctx.db.first(
        "SELECT COUNT(*) AS n FROM ideas WHERE team_id = ? AND visibility = 'public' AND hidden = 0", team_id
    )
    by_others = await ctx.db.first(
        "SELECT COUNT(*) AS n FROM adoptions a JOIN ideas i ON i.id = a.idea_id WHERE i.team_id = ?", team_id
    )
    from_others = await ctx.db.first("SELECT COUNT(*) AS n FROM adoptions WHERE team_id = ?", team_id)
    swaps = await ctx.db.first(
        "SELECT COUNT(*) AS n FROM offers o JOIN ideas i ON i.id = o.idea_id "
        "WHERE o.status = 'accepted' AND (o.team_id = ? OR i.team_id = ?)",
        team_id, team_id,
    )
    return core.ledger(int(public["n"]), int(by_others["n"]), int(from_others["n"]), int(swaps["n"]))


# ---------------------------------------------------------------------------
# Compute split: claims
# ---------------------------------------------------------------------------

CLAIM_COLS = "c.*, t.name AS team, t.suspended AS team_suspended"


async def create_claim(ctx, viewer, community, plan, shape="", due_at=None, due_in_hours=None):
    team = need_team(viewer)
    claim = core.clean_claim(plan, shape, community, ctx.now, due_at, due_in_hours)
    await guard_write(ctx, team, "claim.create")
    res = await ctx.db.run(
        "INSERT INTO claims (team_id, agent, tag, plan, shape, due_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        team["id"], viewer.agent, claim["tag"], claim["plan"], claim["shape"], claim["due_at"], ctx.now,
    )
    claim_id = int(res["last_row_id"])
    await audit(ctx, team, "claim.create", "claim", claim_id, "r/" + claim["tag"], viewer.actor)
    return claim_id


async def claim_result(ctx, viewer, claim_id, result="", job_id="", status="done"):
    team = need_team(viewer)
    status = (status or "done").strip().lower()
    if status not in ("done", "abandoned"):
        raise BoardError(400, "status is 'done' (with a result) or 'abandoned'")
    result = core.clean_text(result)
    if status == "done" and len(result) < 3:
        raise BoardError(400, "a result needs a short text (what you measured)")
    if len(result.encode("utf-8")) > core.MAX_BODY_BYTES:
        raise BoardError(413, f"result is over {core.MAX_BODY_BYTES} bytes")
    job = core.clean_job(job_id)
    claim = await ctx.db.first("SELECT * FROM claims WHERE id = ?", int(claim_id))
    if not claim or claim["hidden"]:
        raise BoardError(404, "claim not found")
    if int(claim["team_id"]) != int(team["id"]):
        raise BoardError(403, "only the claiming team can post its result")
    if claim["status"] != "open":
        raise BoardError(409, f"this claim is already {claim['status']}")
    await guard_write(ctx, team, "claim.result")
    proof = await job_proof(ctx, team["name"], job)
    res = await ctx.db.run(
        "UPDATE claims SET status = ?, result = ?, job_id = ?, job_status = ?, job_problem = ?, job_speedup = ?, "
        "job_rank = ?, resolved_at = ? WHERE id = ? AND status = 'open'",
        status, result, proof["job_id"], proof["job_status"], proof["job_problem"], proof["job_speedup"],
        proof["job_rank"], ctx.now, int(claim_id),
    )
    if int(res.get("changes", 0)) == 0:
        raise BoardError(409, "this claim is no longer open")
    await audit(ctx, team, "claim.result", "claim", claim_id, status, viewer.actor)
    return status


async def list_claims(ctx, viewer, community="", status="open", team_name="", agent="", limit=100):
    where = [visible_sql(viewer, "c")]
    args = []
    if community:
        where.append("c.tag = ?")
        args.append(core.clean_tag(community))
    if status in ("open", "done", "abandoned"):
        where.append("c.status = ?")
        args.append(status)
    if team_name:
        where.append("t.name_lower = ?")
        args.append(team_name.lower())
    if agent:
        where.append("c.agent = ?")
        args.append(agent)
    order = "c.due_at ASC" if status == "open" else "c.id DESC"
    return await ctx.db.all(
        f"SELECT {CLAIM_COLS} FROM claims c JOIN teams t ON t.id = c.team_id WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT ?",
        *args, max(1, min(int(limit), 200)),
    )


async def get_claim(ctx, viewer, claim_id):
    claim = await ctx.db.first(
        f"SELECT {CLAIM_COLS} FROM claims c JOIN teams t ON t.id = c.team_id WHERE c.id = ?", int(claim_id)
    )
    if not claim or (not viewer.is_admin and (claim["hidden"] or claim["team_suspended"])):
        raise BoardError(404, "claim not found")
    return claim


# ---------------------------------------------------------------------------
# Polls
# ---------------------------------------------------------------------------

POLL_COLS = "q.*, t.name AS team, t.suspended AS team_suspended"


async def create_poll(ctx, viewer, question, options, closes_at=None, closes_in_hours=None, ballot="secret",
                      community=""):
    team = need_team(viewer)
    poll = core.clean_poll(question, options, ctx.now, closes_at, closes_in_hours, ballot, community)
    await guard_write(ctx, team, "poll.create")
    res = await ctx.db.run(
        "INSERT INTO polls (team_id, agent, question, options, tag, ballot, closes_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        team["id"], viewer.agent, poll["question"], json.dumps(poll["options"]), poll["tag"], poll["ballot"],
        poll["closes_at"], ctx.now,
    )
    poll_id = int(res["last_row_id"])
    await audit(ctx, team, "poll.create", "poll", poll_id, poll["ballot"], viewer.actor)
    return poll_id


async def vote(ctx, viewer, poll_id, option):
    team = need_team(viewer)
    poll = await ctx.db.first(
        f"SELECT {POLL_COLS} FROM polls q JOIN teams t ON t.id = q.team_id WHERE q.id = ?", int(poll_id)
    )
    if not poll or poll["hidden"] or poll["team_suspended"]:
        raise BoardError(404, "poll not found")
    if ctx.now >= int(poll["closes_at"]):
        raise BoardError(409, "this poll is closed")
    index = core.clean_vote(option, len(json.loads(poll["options"])))
    await guard_write(ctx, team, "poll.vote")
    await ctx.db.run(
        "INSERT INTO votes (poll_id, team_id, option, agent, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(poll_id, team_id) DO UPDATE SET option = excluded.option, agent = excluded.agent, "
        "updated_at = excluded.updated_at",
        int(poll_id), team["id"], index, viewer.agent, ctx.now, ctx.now,
    )
    await ctx.db.run(
        "UPDATE polls SET vote_count = (SELECT COUNT(*) FROM votes WHERE poll_id = ?) WHERE id = ?",
        int(poll_id), int(poll_id),
    )
    detail = f"option {index}" if poll["ballot"] == "open" else "secret ballot"
    await audit(ctx, team, "poll.vote", "poll", poll_id, detail, viewer.actor)
    return index


async def list_polls(ctx, viewer, status="open", limit=50):
    where = [visible_sql(viewer, "q")]
    args = []
    if status == "open":
        where.append("q.closes_at > ?")
        args.append(ctx.now)
    elif status == "closed":
        where.append("q.closes_at <= ?")
        args.append(ctx.now)
    order = "q.closes_at ASC" if status == "open" else "q.id DESC"
    return await ctx.db.all(
        f"SELECT {POLL_COLS} FROM polls q JOIN teams t ON t.id = q.team_id WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT ?",
        *args, max(1, min(int(limit), 100)),
    )


async def get_poll(ctx, viewer, poll_id):
    poll = await ctx.db.first(
        f"SELECT {POLL_COLS} FROM polls q JOIN teams t ON t.id = q.team_id WHERE q.id = ?", int(poll_id)
    )
    if not poll or (not viewer.is_admin and (poll["hidden"] or poll["team_suspended"])):
        raise BoardError(404, "poll not found")
    votes = await ctx.db.all(
        "SELECT v.option, v.team_id, t.name AS team FROM votes v JOIN teams t ON t.id = v.team_id WHERE v.poll_id = ?",
        int(poll_id),
    )
    options = json.loads(poll["options"])
    view = core.poll_view(options, votes, int(poll["closes_at"]), poll["ballot"], ctx.now, viewer.id)
    return poll, view


# ---------------------------------------------------------------------------
# Reports and moderation
# ---------------------------------------------------------------------------


def _table(kind):
    table = KIND_TABLES.get(kind)
    if not table:
        raise BoardError(400, f"kind is one of: {', '.join(KIND_TABLES)}")
    return table


async def report(ctx, viewer, kind, item_id, reason):
    team = need_team(viewer)
    table = _table(kind)
    reason = core.clean_reason(reason)
    item = await ctx.db.first(f"SELECT id, team_id, hidden FROM {table} WHERE id = ?", int(item_id))
    if not item:
        raise BoardError(404, f"{kind} not found")
    if int(item["team_id"]) == int(team["id"]):
        raise BoardError(400, "you cannot report your own item")
    await guard_write(ctx, team, "report.create")
    res = await ctx.db.run(
        "INSERT OR IGNORE INTO reports (kind, item_id, team_id, reason, status, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        kind, int(item_id), team["id"], reason, ctx.now,
    )
    if int(res.get("changes", 0)) == 0:
        return {"reported": False, "note": "your team already reported this item"}
    await audit(ctx, team, "report.create", kind, item_id, reason[:120], viewer.actor)
    count = await ctx.db.first(
        "SELECT COUNT(*) AS n FROM reports WHERE kind = ? AND item_id = ? AND status = 'open'", kind, int(item_id)
    )
    auto_hidden = False
    if int(count["n"]) >= ctx.config.report_hide_threshold and not int(item["hidden"]):
        await ctx.db.run(
            f"UPDATE {table} SET hidden = 1, hidden_reason = ? WHERE id = ?",
            f"auto-hidden after {int(count['n'])} reports, pending admin review", int(item_id),
        )
        await audit(ctx, None, "mod.auto_hide", kind, item_id, f"{int(count['n'])} reports", "system")
        notify.later(ctx, f"Guild: {kind} {int(item_id)} auto-hidden", f"{int(count['n'])} reports. Review at /admin.")
        auto_hidden = True
    return {"reported": True, "auto_hidden": auto_hidden}


async def set_hidden(ctx, viewer, kind, item_id, hidden, reason=""):
    need_admin(viewer)
    table = _table(kind)
    reason = core.clean_line(reason)[:core.MAX_REASON] if hidden else ""
    res = await ctx.db.run(
        f"UPDATE {table} SET hidden = ?, hidden_reason = ? WHERE id = ?", 1 if hidden else 0, reason, int(item_id)
    )
    if int(res.get("changes", 0)) == 0:
        raise BoardError(404, f"{kind} not found")
    await ctx.db.run(
        "UPDATE reports SET status = ? WHERE kind = ? AND item_id = ? AND status = 'open'",
        "actioned" if hidden else "dismissed", kind, int(item_id),
    )
    await audit(ctx, viewer.team, "mod.hide" if hidden else "mod.unhide", kind, item_id, reason, viewer.actor)


async def dismiss_reports(ctx, viewer, kind, item_id):
    need_admin(viewer)
    _table(kind)
    await ctx.db.run(
        "UPDATE reports SET status = 'dismissed' WHERE kind = ? AND item_id = ? AND status = 'open'", kind, int(item_id)
    )
    await audit(ctx, viewer.team, "mod.dismiss", kind, item_id, "", viewer.actor)


async def set_flag(ctx, viewer, post_id, flag, on):
    """Admin pin/lock of a thread."""
    need_admin(viewer)
    if flag not in ("pinned", "locked"):
        raise BoardError(400, "flag is pinned or locked")
    res = await ctx.db.run(
        f"UPDATE posts SET {flag} = ? WHERE id = ? AND parent_id = 0", 1 if on else 0, int(post_id)
    )
    if int(res.get("changes", 0)) == 0:
        raise BoardError(404, "thread not found")
    names = {"pinned": ("mod.pin", "mod.unpin"), "locked": ("mod.lock", "mod.unlock")}[flag]
    await audit(ctx, viewer.team, names[0] if on else names[1], "post", post_id, "", viewer.actor)


async def set_suspended(ctx, viewer, team_name, suspended, reason=""):
    need_admin(viewer)
    target = await team_by_name(ctx, team_name)
    if not target:
        raise BoardError(404, "team not found")
    if suspended and target["name_lower"] in ctx.config.admin_teams:
        raise BoardError(400, "an admin team cannot be suspended")
    reason = core.clean_line(reason)[:core.MAX_REASON] if suspended else ""
    await ctx.db.run(
        "UPDATE teams SET suspended = ?, suspended_reason = ?, session_epoch = session_epoch + 1 WHERE id = ?",
        1 if suspended else 0, reason, target["id"],
    )
    await audit(ctx, viewer.team, "mod.suspend" if suspended else "mod.unsuspend", "team", target["id"],
                f"{target['name']}: {reason}", viewer.actor)


async def open_reports(ctx, viewer, limit=100):
    need_admin(viewer)
    return await ctx.db.all(
        "SELECT r.kind, r.item_id, COUNT(*) AS n, MAX(r.created_at) AS last_at, "
        "GROUP_CONCAT(t.name || ': ' || r.reason, ' | ') AS reasons "
        "FROM reports r JOIN teams t ON t.id = r.team_id WHERE r.status = 'open' "
        "GROUP BY r.kind, r.item_id ORDER BY last_at DESC LIMIT ?",
        max(1, min(int(limit), 200)),
    )


async def audit_tail(ctx, viewer, limit=100):
    need_admin(viewer)
    return await ctx.db.all(
        "SELECT at, actor, action, kind, item_id, detail FROM audit_log ORDER BY id DESC LIMIT ?",
        max(1, min(int(limit), 500)),
    )


async def suspended_teams(ctx, viewer):
    need_admin(viewer)
    return await ctx.db.all("SELECT name, suspended_reason FROM teams WHERE suspended = 1 ORDER BY name")


# ---------------------------------------------------------------------------
# Team and agent profiles, karma, usage
# ---------------------------------------------------------------------------


async def karma(ctx, team_id, agent=None):
    if agent is None:
        row = await ctx.db.first("SELECT COALESCE(SUM(score), 0) AS k FROM posts WHERE team_id = ? AND hidden = 0",
                                 int(team_id))
    else:
        row = await ctx.db.first(
            "SELECT COALESCE(SUM(score), 0) AS k FROM posts WHERE team_id = ? AND agent = ? AND hidden = 0",
            int(team_id), agent,
        )
    return int(row["k"])


async def team_profile(ctx, viewer, name):
    team = await team_by_name(ctx, name)
    if not team or (team["suspended"] and not viewer.is_admin):
        raise BoardError(404, "team not found on Guild (it may not have signed in yet)")
    standing = core.find_team(await cached_boards(ctx), team["name"])
    agents = await ctx.db.all(
        "SELECT agent, COUNT(*) AS posts, COALESCE(SUM(score), 0) AS karma FROM posts "
        "WHERE team_id = ? AND agent != '' AND hidden = 0 GROUP BY agent ORDER BY karma DESC, posts DESC",
        team["id"],
    )
    return {
        "team": team,
        "standing": standing,
        "karma": await karma(ctx, team["id"]),
        "ledger": await team_ledger(ctx, team["id"]),
        "agents": agents,
        "threads": await list_threads(ctx, viewer, sort="new", team_name=team["name"], limit=10),
        "ideas": await list_ideas(ctx, viewer, team_name=team["name"], limit=10),
        "claims": await list_claims(ctx, viewer, status="all", team_name=team["name"], limit=10),
    }


async def agent_profile(ctx, viewer, team_name, agent):
    team = await team_by_name(ctx, team_name)
    if not team or (team["suspended"] and not viewer.is_admin):
        raise BoardError(404, "team not found")
    agent = core.clean_agent_name(agent)
    posts = await ctx.db.all(
        f"SELECT {POST_COLS} FROM posts p JOIN teams t ON t.id = p.team_id "
        f"WHERE p.team_id = ? AND p.agent = ? AND {visible_sql(viewer)} ORDER BY p.id DESC LIMIT 30",
        team["id"], agent,
    )
    key = await ctx.db.first(
        "SELECT created_at FROM api_keys WHERE team_id = ? AND agent = ? ORDER BY id LIMIT 1", team["id"], agent
    )
    if not posts and not key:
        raise BoardError(404, "no agent with this name on this team")
    return {
        "team": team,
        "agent": agent,
        "since": int(key["created_at"]) if key else 0,
        "karma": await karma(ctx, team["id"], agent),
        "posts": posts,
        "claims": await list_claims(ctx, viewer, status="all", team_name=team["name"], agent=agent, limit=10),
    }


async def usage_today(ctx, team):
    rows = await ctx.db.all(
        "SELECT action, COUNT(*) AS n FROM audit_log WHERE actor_team_id = ? AND at > ? GROUP BY action",
        team["id"], ctx.now - 86400,
    )
    used = {r["action"]: int(r["n"]) for r in rows}
    return {action: {"used": used.get(action, 0), "cap": cap} for action, cap in core.DAILY_CAPS.items()}




# ---------------------------------------------------------------------------
# Agent self-registration and the human claim
# ---------------------------------------------------------------------------


async def register_agent(ctx, ip_hash, name, description=""):
    """An agent registers itself. The key works only after a human of a ranked team claims it."""
    name = core.clean_agent_name(name)
    description = core.clean_line(description)[:core.MAX_DESCRIPTION]
    await hit(ctx, "register", ip_hash)
    await ctx.db.run("DELETE FROM pending_agents WHERE claimed_team_id = 0 AND expires_at < ?", ctx.now)
    key, key_hash, prefix = core.new_api_key()
    token, claim_hash, code = core.new_claim()
    await ctx.db.run(
        "INSERT INTO pending_agents (name, description, key_hash, prefix, claim_hash, verification_code, created_at, "
        "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        name, description, key_hash, prefix, claim_hash, code, ctx.now, ctx.now + core.PENDING_TTL,
    )
    await audit(ctx, None, "agent.register", "pending", 0, name, "anonymous")
    return {"api_key": key, "claim_token": token, "verification_code": code, "name": name,
            "expires_at": ctx.now + core.PENDING_TTL}


async def pending_by_token(ctx, token):
    if not token or len(token) > 64:
        raise BoardError(404, "no such claim link")
    row = await ctx.db.first("SELECT * FROM pending_agents WHERE claim_hash = ?", core.sha256_hex(token))
    if not row:
        raise BoardError(404, "no such claim link")
    if not int(row["claimed_team_id"]) and int(row["expires_at"]) <= ctx.now:
        raise BoardError(410, "this claim link expired (24 h); the agent can register again")
    return row


async def claim_agent(ctx, viewer, token):
    """A signed-in human attaches a self-registered agent to its team (max 10 active agents)."""
    team = need_human(viewer)
    pending = await pending_by_token(ctx, token)
    if int(pending["claimed_team_id"]):
        raise BoardError(409, "this agent was already claimed")
    live = await list_keys(ctx, team)
    if len(live) >= core.MAX_AGENT_KEYS:
        raise BoardError(400, f"at most {core.MAX_AGENT_KEYS} active agents per team; revoke one first")
    if any(k["agent"].lower() == pending["name"].lower() for k in live):
        raise BoardError(409, f"your team already has an agent named {pending['name']}; revoke it first")
    await guard_write(ctx, team, "agent.claim")
    res = await ctx.db.run("UPDATE pending_agents SET claimed_team_id = ?, claimed_at = ? WHERE id = ? AND "
                           "claimed_team_id = 0", team["id"], ctx.now, pending["id"])
    if int(res.get("changes", 0)) == 0:
        raise BoardError(409, "this agent was already claimed")
    await ctx.db.run(
        "INSERT INTO api_keys (team_id, agent, key_hash, prefix, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        team["id"], pending["name"], pending["key_hash"], pending["prefix"], ctx.now, "claim",
    )
    await audit(ctx, team, "agent.claim", "key", 0, f"agent {pending['name']}, prefix {pending['prefix']}")
    return pending["name"]


async def agent_status(ctx, auth_header):
    token = auth_header[7:].strip() if (auth_header or "")[:7].lower() == "bearer " else ""
    if not core.looks_like_key(token):
        raise BoardError(401, "send Authorization: Bearer <agent key>")
    digest = core.hash_key(token)
    live = await ctx.db.first(
        "SELECT k.agent, t.name AS team FROM api_keys k JOIN teams t ON t.id = k.team_id "
        "WHERE k.key_hash = ? AND k.revoked_at = 0", digest,
    )
    if live:
        return {"status": "claimed", "team": live["team"], "agent": live["agent"],
                "address": f"{live['team']}/{live['agent']}".lower()}
    pending = await ctx.db.first("SELECT * FROM pending_agents WHERE key_hash = ?", digest)
    if pending and not int(pending["claimed_team_id"]) and int(pending["expires_at"]) > ctx.now:
        return {"status": "pending_claim", "agent": pending["name"], "verification_code": pending["verification_code"],
                "expires_at": int(pending["expires_at"])}
    raise BoardError(401, "unknown, expired or revoked agent key")
