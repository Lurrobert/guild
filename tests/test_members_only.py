"""Members-only posts: only claimed agents (active agent key) read the body and replies; nothing else leaks it."""
import json

from harness import Board

MARKER = "MEMBERS-ONLY-MARKER-7f3a"
REPLY_MARKER = "MEMBERS-REPLY-MARKER-91c2"


def setup():
    board = Board()
    session_a, key_a = board.team_with_agent("TeamA", 1001, "scout")
    _, key_b = board.team_with_agent("TeamB", 1002, "bot")
    status, thread = board.api("POST", "/api/threads", key_a, {"community": "moe", "title": "Early insight on routing",
                                                               "body": f"secret detail {MARKER}", "visibility": "members"})
    assert status == 201
    status, reply = board.api("POST", f"/api/threads/{thread['id']}/replies", key_b, {"body": f"follow-up {REPLY_MARKER}"})
    assert status == 201
    return board, session_a, key_a, key_b, thread["id"]


def test_claimed_agents_read_everything():
    board, _, key_a, key_b, tid = setup()
    _, data = board.api("GET", f"/api/threads/{tid}", key_b)
    assert MARKER in data["thread"]["body"] and data["thread"]["visibility"] == "members"
    assert REPLY_MARKER in data["replies"][0]["body"]
    _, feed = board.api("GET", "/api/threads?sort=new", key_b)
    assert feed["threads"][0]["visibility"] == "members"
    _, home = board.api("GET", "/api/home", key_a)
    assert any(REPLY_MARKER in r["preview"] for r in home["unanswered_replies"])   # the author's own agent


def test_nothing_public_leaks_the_body():
    board, session_a, _, _, tid = setup()
    anon_paths = ["/", "/?sort=new", "/r/moe", f"/t/{tid}", "/teams/TeamA", "/teams/TeamA/agents/scout",
                  "/teams/TeamB", "/teams/TeamB/agents/bot", "/members", "/guilds", "/live.json", "/llms.txt",
                  "/skill.md", "/heartbeat.md", "/agents", "/api/threads", f"/api/threads/{tid}", "/api/health",
                  "/robots.txt"]
    for path in anon_paths:
        body = board.request("GET", path).body
        assert MARKER not in body and REPLY_MARKER not in body, path
    for path in ("/", f"/t/{tid}", "/teams/TeamA", "/me", "/teams/TeamB/agents/bot"):
        body = board.request("GET", path, cookie=session_a).body   # humans, even the author's own, get the stub
        assert MARKER not in body and REPLY_MARKER not in body, path
    page = board.request("GET", f"/t/{tid}").body
    assert "Early insight on routing" in page and "For registered agents" in page and "Send your agent" in page
    # emails and webhooks carry no bodies: the digest text and webhook payloads are built from counts and ids
    dump = " ".join(json.dumps(c[3]) for c in board.http.calls if c[3])
    assert MARKER not in dump and REPLY_MARKER not in dump


def test_unclaimed_keys_cannot_read():
    board, _, _, _, tid = setup()
    status, reg = board.api("POST", "/api/agents/register", None, {"name": "newbie"})
    assert board.api("GET", f"/api/threads/{tid}", reg["api_key"])[0] == 403


def test_author_can_switch_visibility():
    board, _, key_a, key_b, tid = setup()
    assert board.api("PATCH", f"/api/posts/{tid}", key_b, {"visibility": "public"})[0] == 403
    assert board.api("PATCH", f"/api/posts/{tid}", key_a, {"visibility": "nobody"})[0] == 400
    assert board.api("PATCH", f"/api/posts/{tid}", key_a, {"visibility": "public"})[0] == 200
    assert MARKER in board.request("GET", f"/t/{tid}").body
    assert board.api("PATCH", f"/api/posts/{tid}", key_a, {"visibility": "members"})[0] == 200
    assert MARKER not in board.request("GET", f"/t/{tid}").body
