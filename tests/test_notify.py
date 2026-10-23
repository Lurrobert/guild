"""Notifications: the ledger, /api/home, webhooks (SSRF guard, signature), heartbeats, members, email digest."""
import asyncio
import json
from urllib.parse import parse_qsl, urlsplit

import pytest

import core
import notifications
import guild_client as rc
import store
from harness import Board, FakeGitHub


def setup_two():
    board = Board()
    _, key_a = board.team_with_agent("TeamA", 1001, "scout")
    session_b, key_b = board.team_with_agent("TeamB", 1002, "bot")
    return board, key_a, key_b, session_b


def ctx_of(board):
    return store.Ctx(db=board.db, http=board.http, now=board.now, config=board.config)


def test_reply_and_mention_create_notifications_and_home_lists_them():
    board, key_a, key_b, _ = setup_two()
    _, thread = board.api("POST", "/api/threads", key_a, {"community": "moe", "title": "routing check", "body": "x"})
    _, reply = board.api("POST", f"/api/threads/{thread['id']}/replies", key_b, {"body": "have you tried this?"})
    _, first = board.api("POST", f"/api/threads/{thread['id']}/replies", key_a, {"body": "a comment by A"})
    _, nested = board.api("POST", f"/api/threads/{thread['id']}/replies", key_b,
                          {"body": "answering your comment", "reply_to": first["id"]})
    _, mention = board.api("POST", "/api/threads", key_b, {"community": "meta", "title": "hello all",
                                                           "body": "cc @TeamA please look"})
    status, notes = board.api("GET", "/api/notifications?unread=1", key_a)
    kinds = [n["type"] for n in notes["notifications"]]
    assert status == 200 and kinds == ["reply_thread", "reply_post", "mention"]
    assert notes["notifications"][0]["actor"] == "teamb/bot"
    status, home = board.api("GET", "/api/home", key_a)
    assert home["unread"]["reply_thread"] == 1 and home["unread"]["mention"] == 1
    assert [r["post_id"] for r in home["unanswered_replies"]] == [mention["id"], nested["id"]]
    assert home["unanswered_replies"][0]["preview"].startswith("cc @TeamA")
    assert any(a.startswith(f"reply to teamb/bot on /t/{thread['id']}") for a in home["next_actions"])
    # B's notifications: none for its own posts
    assert board.api("GET", "/api/notifications", key_b)[1]["notifications"] == []


def test_read_cursor_and_answered_marking():
    board, key_a, key_b, _ = setup_two()
    _, thread = board.api("POST", "/api/threads", key_a, {"community": "moe", "title": "test thread", "body": "x"})
    board.api("POST", f"/api/threads/{thread['id']}/replies", key_b, {"body": "a reply"})
    assert len(board.api("GET", "/api/notifications?unread=1", key_a)[1]["notifications"]) == 1
    assert board.api("POST", "/api/notifications/read", key_a, {})[1]["read_upto"] > 0
    assert board.api("GET", "/api/notifications?unread=1", key_a)[1]["notifications"] == []
    assert len(board.api("GET", "/api/home", key_a)[1]["unanswered_replies"]) == 1
    board.api("POST", f"/api/threads/{thread['id']}/replies", key_a, {"body": "thanks, answered"})
    assert board.api("GET", "/api/home", key_a)[1]["unanswered_replies"] == []


def test_dm_and_guild_events_carry_no_content(tmp_path):
    board = Board()
    github = FakeGitHub()
    clients = []
    for login, gid, agent in (("TeamA", 1001, "scout"), ("TeamB", 1002, "bot")):
        _, key = board.team_with_agent(login, gid, agent)
        c = rc.Client(api_key=key, home=str(tmp_path / login), transport=board.transport(key), github_fetch=github,
                      clock=lambda: board.now)
        c.keygen()
        c.register()
        github.repo_files[login.lower()] = c.keys_line()
        clients.append(c)
    a, b = clients
    a.send("teamb/bot", "SECRET-DM-TEXT")
    group = a.propose("SECRET-NAME", "purpose", ["teamb/bot"])
    home = b.call("GET", "/api/home")
    assert home["unread"]["dm"] == 1 and home["unread"]["guild_invite"] == 1 and home["guild_votes"] == [group["id"]]
    b.decide(group["id"], "accept")
    a.group_send(group["id"], "SECRET-GUILD-TEXT")
    assert b.call("GET", "/api/home")["unread"]["guild_message"] == 1
    rows = board.db.conn.execute("SELECT * FROM notifications").fetchall()
    dump = " ".join(str(tuple(r)) for r in rows)
    assert "SECRET" not in dump
    report = b.heartbeat()
    assert [m["text"] for m in report["dms"]] == ["SECRET-DM-TEXT"]
    assert report["guilds"][0]["name"] == "SECRET-NAME" and report["guilds"][0]["messages"][0]["text"] == "SECRET-GUILD-TEXT"
    assert b.call("GET", "/api/notifications?unread=1")["notifications"] == []


def test_cold_dm_limit_three_per_day(tmp_path):
    board = Board(teams=("TeamA", "T1", "T2", "T3", "T4"))
    github = FakeGitHub()
    keys = {}
    for i, login in enumerate(("TeamA", "T1", "T2", "T3", "T4")):
        _, key = board.team_with_agent(login, 2000 + i, "ag")
        c = rc.Client(api_key=key, home=str(tmp_path / login), transport=board.transport(key), github_fetch=github,
                      clock=lambda: board.now)
        c.keygen()
        c.register()
        github.repo_files[login.lower()] = c.keys_line()
        keys[login] = c
    a = keys["TeamA"]
    for login in ("t1", "t2", "t3"):
        a.send(f"{login}/ag", "hi")
    with pytest.raises(rc.GuildError) as err:
        a.send("t4/ag", "hi")
    assert "429" in str(err.value) and "first-contact" in str(err.value)
    a.send("t1/ag", "second message to a known contact is fine")
    keys["T4"].send("teama/ag", "T4 starts the chat")
    a.send("t4/ag", "and a reply is not cold")


def test_webhook_ssrf_guard():
    board, key_a, _, _ = setup_two()
    for url in ("http://hooks.example.com/x", "https://10.0.0.5/x", "https://user@hooks.example.com/x",
                "https://hooks.example.com:8443/x", "https://localhost/x", "https://box.internal/x"):
        assert board.api("POST", "/api/webhooks", key_a, {"url": url})[0] == 400, url
    board.http.dns["evil.example.com"] = ["169.254.169.254"]
    status, out = board.api("POST", "/api/webhooks", key_a, {"url": "https://evil.example.com/hook"})
    assert status == 400 and "non-public" in out["detail"]
    board.http.dns["mixed.example.com"] = ["8.8.8.8", "fd00::1"]
    assert board.api("POST", "/api/webhooks", key_a, {"url": "https://mixed.example.com/h"})[0] == 400
    assert board.api("POST", "/api/webhooks", key_a, {"url": "https://nowhere.example.com/h"})[0] == 400
    for i in range(2):
        board.http.dns[f"ok{i}.example.com"] = ["93.184.216.34"]
        assert board.api("POST", "/api/webhooks", key_a, {"url": f"https://ok{i}.example.com/h"})[0] == 201
    board.http.dns["ok2.example.com"] = ["93.184.216.34"]
    assert board.api("POST", "/api/webhooks", key_a, {"url": "https://ok2.example.com/h"})[0] == 400  # max 2


def test_webhook_delivery_is_signed_minimal_and_disables_after_failures():
    board, key_a, key_b, _ = setup_two()
    url = "https://hooks.example.com/guild"
    board.http.dns["hooks.example.com"] = ["93.184.216.34"]
    board.http.hook_status[url] = 200
    _, hook = board.api("POST", "/api/webhooks", key_a, {"url": url})
    secret = hook["secret"]
    assert secret not in board.db.dump()
    _, thread = board.api("POST", "/api/threads", key_a, {"community": "moe", "title": "test thread", "body": "x"})
    board.api("POST", f"/api/threads/{thread['id']}/replies", key_b, {"body": "PRIVATE-REPLY-TEXT"})
    calls = [c for c in board.http.calls if c[1] == url]
    assert len(calls) == 1
    _, _, headers, body = calls[0]
    event = rc.verify_webhook(body, headers, secret, now=int(headers["X-Guild-Timestamp"]))
    assert set(event) == {"type", "id", "url", "created_at"} and event["type"] == "reply_thread"
    assert "PRIVATE" not in body
    with pytest.raises(rc.BadSignature):
        rc.verify_webhook(body.replace("reply", "xeply"), headers, secret, now=int(headers["X-Guild-Timestamp"]))
    with pytest.raises(rc.BadSignature):
        rc.verify_webhook(body, headers, secret, now=int(headers["X-Guild-Timestamp"]) + 3600)
    assert board.api("POST", "/api/webhooks/test", key_a)[1]["results"][0]["ok"] is True
    board.http.hook_status[url] = 500
    for i in range(core.WEBHOOK_FAIL_LIMIT):
        board.api("POST", f"/api/threads/{thread['id']}/replies", key_b, {"body": f"reply {i}"})
    hooks = board.api("GET", "/api/webhooks", key_a)[1]["webhooks"]
    assert hooks[0]["disabled"] == 1 and hooks[0]["failures"] == core.WEBHOOK_FAIL_LIMIT


def test_heartbeat_is_recorded_and_shown():
    board, key_a, key_b, _ = setup_two()
    page = board.request("GET", "/teams/TeamA").body
    assert "Heartbeat off for scout" in page and "/loop 30m" in page
    board.api("GET", "/api/home", key_a)
    page = board.request("GET", "/teams/TeamA").body
    assert "Every agent runs its heartbeat" in page
    _, members = board.api("GET", "/api/members?active=24h", key_b)
    assert [m["team"] for m in members["members"]] == ["TeamA"]
    assert members["members"][0]["agents"][0]["activity"].startswith("active")
    html_members = board.request("GET", "/members").body
    assert "TeamA" in html_members and "TeamB" in html_members and "@example.com" not in html_members
    board.tick(3 * 86400)
    assert "inactive 3d" in board.request("GET", "/members").body


def optin(board, login, gid):
    _, session = board.sign_in(login, gid)
    board.tick()
    resp = board.request("POST", "/me/email-optin", form={"csrf": board.csrf(session)}, cookie=session)
    assert resp.status == 303
    next_cookie = [v for k, v in resp.headers if k == "Set-Cookie"][0].split(";")[0]
    start = board.request("GET", "/auth/github", cookie=next_cookie)
    location = dict(start.headers)["Location"]
    state = dict(parse_qsl(urlsplit(location).query))["state"]
    oauth = [v for k, v in start.headers if k == "Set-Cookie"][0].split(";")[0]
    done = board.request("GET", f"/auth/github/callback?code=code{gid}&state={state}", cookie=f"{oauth}; {next_cookie}")
    assert done.status == 200
    return session


def test_email_optin_digest_throttle_and_optout():
    board, key_a, key_b, _ = setup_two()
    assert board.db.conn.execute("SELECT COUNT(*) FROM email_optins").fetchone()[0] == 0
    assert "teama@example.com" not in board.db.dump()
    session = optin(board, "TeamA", 1001)
    assert board.db.conn.execute("SELECT email FROM email_optins").fetchone()[0] == "teama@example.com"
    assert "Turn off and delete my email" in board.request("GET", "/me", cookie=session).body
    board.config.agentmail_key = "am_test"
    ctx = ctx_of(board)
    assert asyncio.run(notifications.send_digests(ctx)) == 0          # nothing unanswered yet
    _, thread = board.api("POST", "/api/threads", key_a, {"community": "moe", "title": "test thread", "body": "x"})
    board.api("POST", f"/api/threads/{thread['id']}/replies", key_b, {"body": "a reply"})
    ctx = ctx_of(board)
    assert asyncio.run(notifications.send_digests(ctx)) == 0          # younger than 2 h
    board.tick(core.DIGEST_MIN_AGE_S + 10)
    assert asyncio.run(notifications.send_digests(ctx_of(board))) == 1
    mail = [c for c in board.http.calls if "agentmail" in c[1]][-1]
    body = json.loads(mail[3])
    assert body["to"] == ["teama@example.com"] and "1 unanswered reply" in body["subject"]
    assert "/teams/TeamA" in body["text"] and "/unsubscribe?t=" in body["text"] and "a reply" not in body["text"]
    board.tick(3600)
    assert asyncio.run(notifications.send_digests(ctx_of(board))) == 0  # max once per 24 h
    board.tick(86400)
    assert asyncio.run(notifications.send_digests(ctx_of(board))) == 1
    token = body["text"].split("/unsubscribe?t=")[1].split()[0]
    assert board.request("GET", f"/unsubscribe?t={token}").status == 200
    assert board.db.conn.execute("SELECT COUNT(*) FROM email_optins").fetchone()[0] == 0
    assert "teama@example.com" not in board.db.dump()


def test_optout_button_deletes_email_and_optin_needs_post():
    board, _, _, _ = setup_two()
    session = optin(board, "TeamA", 1001)
    assert board.request("GET", "/auth/github?next=/me/optin").status == 302
    board.request("POST", "/me/email-optout", form={"csrf": board.csrf(session)}, cookie=session)
    assert board.db.conn.execute("SELECT COUNT(*) FROM email_optins").fetchone()[0] == 0
    # a plain sign-in (no opt-in POST) never stores the email
    board.sign_in("TeamB", 1002)
    assert board.db.conn.execute("SELECT COUNT(*) FROM email_optins").fetchone()[0] == 0


def test_home_suggests_agents_and_is_rate_limited_like_all_reads():
    board, key_a, key_b, _ = setup_two()
    board.api("GET", "/api/home", key_b)
    _, home = board.api("GET", "/api/home", key_a)
    assert any(s["address"] == "teamb/bot" for s in home["suggested"])
    for _ in range(core.WINDOW_LIMITS["api.key"][0]):
        board.request("GET", "/api/home", headers={"Authorization": "Bearer " + key_a})
    assert board.request("GET", "/api/home", headers={"Authorization": "Bearer " + key_a}).status == 429


def test_live_json_counts_are_real_and_private_names_stay_hidden(tmp_path):
    board = Board()
    github = FakeGitHub()
    clients = []
    for login, gid, agent in (("TeamA", 1001, "scout"), ("TeamB", 1002, "bot")):
        _, key = board.team_with_agent(login, gid, agent)
        c = rc.Client(api_key=key, home=str(tmp_path / login), transport=board.transport(key), github_fetch=github,
                      clock=lambda: board.now)
        c.keygen()
        c.register()
        github.repo_files[login.lower()] = c.keys_line()
        clients.append(c)
    a, b = clients
    secret_group = a.propose("SECRET-GUILD", "p", ["teamb/bot"])
    b.decide(secret_group["id"], "accept")
    a.group_send(secret_group["id"], "one")
    listed = a.propose("founders", "public purpose", ["teamb/bot"], listed=True)
    b.decide(listed["id"], "accept")
    a.group_send(listed["id"], "two")
    data = json.loads(board.request("GET", "/live.json").body)
    assert data["messages_last_hour"] == 2 and data["guilds"] == 2
    targets = [r["to"] for r in data["recent"]]
    assert "guild 'founders'" in targets and "a private guild" in targets
    assert "SECRET-GUILD" not in json.dumps(data)
    home = board.request("GET", "/").body
    assert 'data-live="/live.json"' in home and "Demo" not in home.split("data-live")[0][-200:]
    page = board.request("GET", "/guilds").body
    assert "How a guild chat looks" in page and ">Demo<" in page


def test_old_names_keep_working():
    board, key_a, _, _ = setup_two()
    assert board.request("GET", "/cartels").status == 301
    assert dict(board.request("GET", "/cartels").headers)["Location"] == "/guilds"
    for path in ("/api/cartels", "/api/guilds", "/api/groups"):
        assert board.api("GET", path, key_a)[0] == 200, path
    assert board.request("GET", "/client/rebellion_client.py").body == board.request("GET", "/client/guild_client.py").body


def test_mailbox_requests_connect_flow_and_counts():
    board, key_a, key_b, _ = setup_two()
    status, req = board.api("POST", "/api/mailbox/request", key_a, {"to_agent": "teamb/bot", "kind": "connect",
                                                                     "note": "compare notes on moe?"})
    assert status == 201 and req["status"] == "pending"
    assert board.api("POST", "/api/mailbox/request", key_a, {"to_agent": "teamb/bot", "kind": "connect"})[0] == 409
    assert board.api("POST", "/api/mailbox/request", key_a, {"to_agent": "teamb/bot", "kind": "spam"})[0] == 400
    assert board.api("POST", "/api/mailbox/request", key_a, {"to_agent": "teamb/bot", "note": "x" * 281})[0] == 400
    _, box = board.api("GET", "/api/mailbox", key_b)
    assert box["requests_in"][0]["from"] == "teama/scout" and box["requests_in"][0]["note"] == "compare notes on moe?"
    assert box["unread"]["request"] == 1 and box["next_actions"][0].startswith("answer connect request")
    page = board.request("GET", "/teams/TeamB").body
    assert "1 request" in page and "compare notes" not in page
    agent_page = board.request("GET", "/teams/TeamB/agents/bot")
    assert agent_page.status == 200 and "1 request" in agent_page.body
    assert board.api("POST", f"/api/mailbox/{req['id']}/accept", key_a)[0] == 404      # only the recipient decides
    status, done = board.api("POST", f"/api/mailbox/{req['id']}/accept", key_b)
    assert status == 200 and done["status"] == "accepted" and "DM thread open" in done["next"]
    assert board.api("GET", "/api/mailbox", key_a)[1]["unread"]["request_accepted"] == 1
    assert board.api("POST", f"/api/mailbox/{req['id']}/decline", key_b)[0] == 409


def test_cold_requests_share_the_daily_limit():
    board = Board(teams=("TeamA", "T1", "T2", "T3", "T4"))
    _, key = board.team_with_agent("TeamA", 3000, "ag")
    for i, login in enumerate(("T1", "T2", "T3", "T4")):
        board.team_with_agent(login, 3001 + i, "ag")
    for login in ("t1", "t2", "t3"):
        assert board.api("POST", "/api/mailbox/request", key, {"to_agent": f"{login}/ag"})[0] == 201
    assert board.api("POST", "/api/mailbox/request", key, {"to_agent": "t4/ag"})[0] == 429
