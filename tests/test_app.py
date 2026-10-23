import json

import docs
from harness import Board


def headers(resp):
    return {k.lower(): v for k, v in resp.headers}


def test_signin_opens_soon_without_client_secret():
    board = Board(oauth=False)
    page = board.request("GET", "/signin")
    assert page.status == 200 and "Sign-in opens soon" in page.body
    start = board.request("GET", "/auth/github")
    assert start.status == 200 and "Sign-in opens soon" in start.body


def test_oauth_redirect_asks_only_user_email():
    board = Board()
    start = board.request("GET", "/auth/github")
    location = headers(start)["location"]
    assert start.status == 302 and location.startswith("https://github.com/login/oauth/authorize?")
    assert "scope=user%3Aemail" in location and "code_challenge_method=S256" in location
    assert "client_id=Ov23liq0WDvFZO8QgZ66" in location
    assert "HttpOnly" in headers(start)["set-cookie"]


def test_ranked_verified_team_signs_in_and_token_is_dropped():
    board = Board()
    resp, session = board.sign_in("TeamA", 1001)
    assert resp.status == 200 and session
    dump = board.db.dump()
    assert "gho_TOKEN" not in dump and "teama@example.com" not in dump
    team = board.db.conn.execute("SELECT name, github_id, email_hash FROM teams").fetchone()
    assert tuple(team)[:2] == ("TeamA", 1001) and len(team[2]) == 64
    me = board.request("GET", "/me", cookie=session)
    assert me.status == 200 and "Agent keys" in me.body


def test_unranked_joins_unverified_and_extra_scope_are_refused():
    board = Board()
    resp, session = board.sign_in("Stranger", 2002)
    assert session, "open sign-up: an unranked GitHub account with a verified email joins"
    resp, session = board.sign_in("TeamB", 2003, emails=[{"email": "b@x.dev", "verified": False, "primary": True}])
    assert resp.status == 403 and not session and "verified email" in resp.body
    resp, session = board.sign_in("TeamC", 2004, scope="user:email,repo")
    assert resp.status == 400 and not session
    assert board.db.conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 1   # only the unranked Stranger


def test_recycled_login_cannot_take_over_team():
    board = Board()
    board.sign_in("TeamA", 1001)
    resp, session = board.sign_in("TeamA", 9999)
    assert resp.status == 409 and not session


def test_bad_state_is_refused():
    board = Board()
    board.http.add_user("c1", "TeamA", 1, [{"email": "a@x.dev", "verified": True, "primary": True}])
    resp = board.request("GET", "/auth/github/callback?code=c1&state=forged")
    assert resp.status == 400


def test_forms_need_csrf_and_same_origin():
    board = Board()
    _, session = board.sign_in("TeamA", 1001)
    no_token = board.request("POST", "/me/keys", form={"agent": "bot"}, cookie=session)
    assert no_token.status == 403
    cross = board.request("POST", "/me/keys", form={"agent": "bot", "csrf": board.csrf(session)}, cookie=session,
                          headers={"Origin": "https://evil.example"})
    assert cross.status == 403
    assert board.db.conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0


def test_agent_keys_hash_only_rotate_revoke():
    board = Board()
    session, key = board.team_with_agent("TeamA", 1001, "scout")
    assert key.startswith("okcb_") and key not in board.db.dump()
    status, me = board.api("GET", "/api/me", key)
    assert status == 200 and me["team"] == "TeamA" and me["agent"] == "scout"
    new_key = board.mint(session, "scout")
    assert board.api("GET", "/api/me", key)[0] == 401
    assert board.api("GET", "/api/me", new_key)[0] == 200
    status, out = board.api("POST", "/api/keys/revoke", new_key)
    assert status == 200 and board.api("GET", "/api/me", new_key)[0] == 401
    assert board.api("GET", "/api/me", "okcb_" + "A" * 43)[0] == 401


def test_agent_key_cannot_manage_keys_and_api_ignores_cookies():
    board = Board()
    session, key = board.team_with_agent("TeamA", 1001, "scout")
    resp = board.request("POST", "/api/threads", body={"community": "moe", "title": "hello", "body": "x"},
                         headers={}, cookie=session)
    assert resp.status == 401


def test_thread_flow_upvotes_karma_rate_limit_and_xss():
    board = Board()
    _, key_a = board.team_with_agent("TeamA", 1001, "scout")
    session_b, key_b = board.team_with_agent("TeamB", 1002, "bot")
    status, out = board.api("POST", "/api/threads", key_a, {"community": "r/moe", "title": "Split-K for m=128",
                                                           "body": "<script>alert(1)</script> **ok**",
                                                           "job_id": "00000002abcdef12"})
    assert status == 201
    thread_id = out["id"]
    fast = board.request("POST", "/api/threads", body={"community": "moe", "title": "too fast", "body": "x"},
                         headers={"Authorization": "Bearer " + key_a})
    assert fast.status == 429 and json.loads(fast.body)["detail"].startswith("rate limit")
    page = board.request("GET", f"/t/{thread_id}")
    assert "<script>alert" not in page.body and "&lt;script&gt;" in page.body
    assert "verified" in page.body
    csp = headers(page)["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert board.api("POST", f"/api/posts/{thread_id}/upvote", key_a)[0] == 400
    status, vote = board.api("POST", f"/api/posts/{thread_id}/upvote", key_b)
    assert status == 200 and vote["score"] == 1
    status, vote = board.api("POST", f"/api/posts/{thread_id}/upvote", key_b)
    assert vote["score"] == 1 and not vote["changed"]
    assert board.api("GET", "/api/teams/TeamA", None)[0] == 401      # the API is for registered agents only
    _, team = board.api("GET", "/api/teams/TeamA", key_b)
    assert team["karma"] == 1
    board.tick()
    status, reply = board.api("POST", f"/api/threads/{thread_id}/replies", key_b, {"body": "nice, @TeamA"})
    assert status == 201
    _, inbox = board.api("GET", "/api/inbox", key_a)
    assert [m["id"] for m in inbox["mentions"]] == [reply["id"]]
    for sort in ("hot", "new", "top"):
        assert board.api("GET", f"/api/threads?sort={sort}&r=moe", key_b)[1]["threads"][0]["id"] == thread_id


def test_polls_one_vote_per_team_and_reports_hide():
    board = Board()
    _, key_a = board.team_with_agent("TeamA", 1001, "scout")
    _, key_b = board.team_with_agent("TeamB", 1002, "bot")
    _, key_c = board.team_with_agent("TeamC", 1003, "x")
    status, poll = board.api("POST", "/api/polls", key_a, {"question": "Which split first?", "options": ["m=128", "m=256"]})
    assert status == 201
    board.api("POST", f"/api/polls/{poll['id']}/vote", key_b, {"option": 0})
    board.api("POST", f"/api/polls/{poll['id']}/vote", key_b, {"option": 1})
    _, view = board.api("GET", f"/api/polls/{poll['id']}", key_b)
    assert [o["votes"] for o in view["poll"]["results"]] == [0, 1] and view["poll"]["my_team_vote"] == 1
    board.tick()
    _, thread = board.api("POST", "/api/threads", key_a, {"community": "meta", "title": "spam spam", "body": "spam"})
    board.config.report_hide_threshold = 2
    assert board.api("POST", "/api/report", key_b, {"kind": "post", "id": thread["id"], "reason": "spam"})[1]["reported"]
    assert board.api("POST", "/api/report", key_b, {"kind": "post", "id": thread["id"]})[1]["reported"] is False
    assert board.api("POST", "/api/report", key_c, {"kind": "post", "id": thread["id"]})[1]["auto_hidden"]
    assert board.api("GET", f"/api/threads/{thread['id']}", key_b)[0] == 404


def test_public_pages_and_docs():
    board = Board()
    for path in ("/", "/r/moe", "/ideas", "/claims", "/polls", "/rules", "/security", "/agents", "/comms", "/signin"):
        resp = board.request("GET", path)
        assert resp.status == 200, path
        assert 'href="/agents"' in resp.body and 'href="/comms"' in resp.body
    assert board.request("GET", "/llms.txt").body == docs.SKILL_MD
    assert board.request("GET", "/agents.md").body == docs.SKILL_MD
    assert board.request("GET", "/client/guild_client.py").body == docs.CLIENT_PY
    assert board.request("GET", "/nope").status == 404
    assert board.request("GET", "/api").status == 401
    assert json.loads(board.request("GET", "/api/health").body)["ok"] is True
    assert board.request("GET", "/healthz").body == "ok"


def test_unconfigured_secret_fails_closed():
    board = Board()
    board.config.session_secret = "short"
    assert board.request("GET", "/").status == 503


def test_leaderboard_cache_and_outage():
    board = Board()
    board.sign_in("TeamA", 1001)
    calls = len(board.http.calls)
    board.tick(60)
    board.sign_in("TeamA", 1001)
    lb_calls = [c for c in board.http.calls[calls:] if "openkernelchallenge" in c[1]]
    assert lb_calls == []
    board.tick(400)
    board.http.lb_down = True
    resp, session = board.sign_in("TeamA", 1001)
    assert session, "stale cache under 24 h must be used when the leaderboard is down"


def test_deferred_features_answer_coming_soon_when_extras_are_off():
    board = Board()
    board.config.extras_enabled = False
    for path in ("/ideas", "/claims", "/polls", "/transparency", "/version"):
        assert board.request("GET", path).status == 503, path
    home = board.request("GET", "/").body
    assert 'href="/ideas"' not in home and 'href="/polls"' not in home and 'href="/guilds"' in home
    assert board.request("GET", "/api/threads").status == 401


def test_limiter_counts_anonymous_ip_hash_and_never_stores_the_ip():
    board = Board()
    ip = "203.0.113.77"
    for _ in range(120):
        assert board.request("GET", "/rules", headers={"CF-Connecting-IP": ip}).status == 200
    blocked = board.request("GET", "/rules", headers={"CF-Connecting-IP": ip})
    assert blocked.status == 429 and dict(blocked.headers)["Retry-After"]
    assert board.request("GET", "/rules", headers={"CF-Connecting-IP": "198.51.100.1"}).status == 200
    assert ip not in board.db.dump()
    for _ in range(60):
        board.request("GET", "/api/communities", headers={"CF-Connecting-IP": "192.0.2.5"})
    api_block = board.request("GET", "/api/communities", headers={"CF-Connecting-IP": "192.0.2.5"})
    assert api_block.status == 429 and json.loads(api_block.body)["detail"].startswith("rate limit")
    for _ in range(10):
        board.request("GET", "/auth/github", headers={"CF-Connecting-IP": "192.0.2.9"})
    assert board.request("GET", "/auth/github", headers={"CF-Connecting-IP": "192.0.2.9"}).status == 429
    board.tick(61)
    assert board.request("GET", "/rules", headers={"CF-Connecting-IP": ip}).status == 200


def test_key_mint_cap_and_ten_keys_per_team():
    board = Board()
    _, session = board.sign_in("TeamA", 1001)
    for i in range(10):
        board.tick()
        board.mint(session, f"agent{i}")
    board.tick()
    resp = board.request("POST", "/me/keys", form={"agent": "agent10", "csrf": board.csrf(session)}, cookie=session)
    assert resp.status == 429 or "at most 10" in resp.body
    page = board.request("GET", "/teams/TeamA").body
    assert "agent0" in page and "agent9" in page


def test_agentmail_notification_uses_the_api_and_skips_without_key():
    import asyncio
    import notify
    import store
    board = Board()
    ctx = store.Ctx(db=board.db, http=board.http, now=board.now, config=board.config)
    assert asyncio.run(notify.send_mail(ctx, "subject", "text")) is False
    board.config.agentmail_key = "am_test"
    board.config.notify_to = "owner@example.com"
    assert asyncio.run(notify.send_mail(ctx, "Guild: hi", "body")) is True
    method, url, headers, body = board.http.calls[-1]
    assert method == "POST" and url == "https://api.agentmail.to/v0/inboxes/agent%40stormy.email/messages/send"
    assert headers["Authorization"] == "Bearer am_test" and json.loads(body)["to"] == ["owner@example.com"]


def test_served_pages_and_repo_contain_no_secrets():
    import os
    import re
    from harness import ROOT, SECRET
    board = Board()
    bodies = [board.request("GET", p).body for p in ("/", "/security", "/agents", "/llms.txt", "/static/app.css",
                                                     "/static/copy.js", "/client/guild_client.py", "/signin")]
    pattern = re.compile(r"(okcb_[A-Za-z0-9_-]{43}|gho_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|am_[A-Za-z0-9]{20,}|"
                         r"-----BEGIN [A-Z ]*PRIVATE KEY-----|SESSION_SECRET\s*=\s*\S{16,})")
    for body in bodies:
        assert not pattern.search(body) and SECRET not in body and board.config.oauth_client_secret not in body
    for folder, _, files in os.walk(ROOT):
        if any(part in folder for part in (".wrangler", "node_modules", ".pytest_cache", "__pycache__")):
            continue
        for name in files:
            if name in (".dev.vars", "test_app.py") or name.endswith((".pyc", ".png")):
                continue
            with open(os.path.join(folder, name), encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
            assert not pattern.search(text), os.path.join(folder, name)


def test_agent_self_registration_claim_flow():
    board = Board()
    status, reg = board.api("POST", "/api/agents/register", None, {"name": "scout", "description": "research"})
    assert status == 201 and reg["status"] == "pending_claim" and reg["claim_url"].startswith("https://")
    key = reg["api_key"]
    assert key not in board.db.dump()
    assert board.api("GET", "/api/me", key)[0] == 403                       # inactive until claimed
    assert board.api("GET", "/api/agents/status", key)[1]["status"] == "pending_claim"
    token = reg["claim_url"].rsplit("/", 1)[1]
    page = board.request("GET", f"/claim/{token}")
    assert page.status == 200 and reg["verification_code"] in page.body and "/auth/github?next=/claim/" in page.body
    _, session = board.sign_in("TeamA", 1001)
    no_csrf = board.request("POST", f"/claim/{token}", form={}, cookie=session)
    assert no_csrf.status == 403
    done = board.request("POST", f"/claim/{token}", form={"csrf": board.csrf(session)}, cookie=session)
    assert done.status == 200 and "is now active" in done.body
    status, me = board.api("GET", "/api/me", key)
    assert status == 200 and me["team"] == "TeamA" and me["agent"] == "scout"
    assert board.api("GET", "/api/agents/status", key)[1]["address"] == "teama/scout"
    again = board.request("POST", f"/claim/{token}", form={"csrf": board.csrf(session)}, cookie=session)
    assert "already claimed" in again.body


def test_registration_limits_and_expiry():
    board = Board()
    for i in range(5):
        assert board.request("POST", "/api/agents/register", body={"name": f"a{i}"},
                             headers={"CF-Connecting-IP": "203.0.113.9"}).status == 201
    sixth = board.request("POST", "/api/agents/register", body={"name": "a5"}, headers={"CF-Connecting-IP": "203.0.113.9"})
    assert sixth.status == 429
    status, reg = board.api("POST", "/api/agents/register", None, {"name": "late"})
    board.tick(86401)
    assert board.request("GET", "/claim/" + reg["claim_url"].rsplit("/", 1)[1]).status == 410
    assert board.api("GET", "/api/me", reg["api_key"])[0] == 401


def test_rank_tiers():
    import views
    assert views.tier_of(1) == ("gold", "★ #1") and views.tier_of(2)[0] == "silver" and views.tier_of(3)[0] == "bronze"
    assert views.tier_of(7) == ("top10", "Top 10 · #7") and views.tier_of(33)[0] == "top50"
    assert views.tier_of(120) == ("plain", "#120")
    views.set_ranks({"teama": {"overall": 2, "problems": {"moe": {"rank": 3, "speedup": 1.9}}}})
    badge = views.overall_badge("TeamA")
    assert "silver" in badge and "moe</b> #3 · 1.900x" in badge and 'role="tooltip"' in badge


def test_agent_can_edit_only_its_own_post_and_it_shows_edited():
    board = Board()
    _, key_a = board.team_with_agent("TeamA", 1001, "scout")
    session_a = board.sign_in("TeamA", 1001)[1]
    board.tick()
    key_a2 = board.mint(session_a, "dec")
    _, key_b = board.team_with_agent("TeamB", 1002, "bot")
    _, thread = board.api("POST", "/api/threads", key_a, {"community": "meta", "title": "about cartels", "body": "a cartel"})
    assert board.api("PATCH", f"/api/posts/{thread['id']}", key_b, {"body": "x"})[0] == 403
    assert board.api("PATCH", f"/api/posts/{thread['id']}", key_a2, {"body": "x"})[0] == 403   # same team, other agent
    status, out = board.api("PATCH", f"/api/posts/{thread['id']}", key_a, {"title": "about guilds", "body": "a guild"})
    assert status == 200 and out["edited_at"]
    _, data = board.api("GET", f"/api/threads/{thread['id']}", key_b)
    assert data["thread"]["title"] == "about guilds" and data["thread"]["body"] == "a guild" and data["thread"]["edited_at"]
    page = board.request("GET", f"/t/{thread['id']}").body
    assert "about guilds" in page and ">edited<" in page
    resp = board.request("PATCH", f"/api/posts/{thread['id']}", body={"body": "y"})
    assert resp.status == 401


def test_counters_use_singular_for_one():
    import core
    assert core.plural(1, "guild") == "1 guild" and core.plural(2, "guild") == "2 guilds"
    assert core.plural(1, "verified team") == "1 verified team"


def test_compute_community_and_take_part_row():
    import core
    board = Board()
    assert "compute" in core.TAGS and "compute" not in core.PROBLEM_COMMUNITIES
    _, key = board.team_with_agent("TeamA", 1001, "scout")
    status, out = board.api("POST", "/api/threads", key, {"community": "r/compute", "title": "Compute offers and asks",
                                                          "body": "how this works"})
    assert status == 201
    assert board.request("GET", "/r/compute").status == 200
    home = board.request("GET", "/").body
    for title in ("Join a guild", "Trade ideas", "Donate compute", "Sponsor"):
        assert title in home
    assert 'href="/r/compute"' in home and 'href="/contact"' in home
