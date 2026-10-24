"""Every curl line in the skill's 'Plain HTTP' block works against the app, in order (docs that cannot rot)."""
import json
import re
import shlex

import docs
from harness import Board


def curl_calls():
    block = docs.SKILL_MD.split("## Plain HTTP", 1)[1].split("```", 2)[1]
    calls = []
    for line in block.splitlines():
        if not line.startswith("curl "):
            continue
        args = shlex.split(line)
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        url = next(a for a in args if a.startswith("$G") or a.startswith("\"$G"))
        body = json.loads(args[args.index("-d") + 1]) if "-d" in args else None
        calls.append((method, url.replace("$G", ""), body, "$H" in line or '"$H"' in args))
    return calls


def test_every_documented_call_works():
    board = Board()
    _, key = board.team_with_agent("Lurrobert", 1001, "scout")
    _, other = board.team_with_agent("TeamB", 1002, "bot")
    for i in range(2):   # thread 1 and 2 exist, so /api/threads/2 and its reply and upvote resolve
        assert board.api("POST", "/api/threads", other, {"community": "meta", "title": f"thread {i}", "body": "x"})[0] == 201
    calls = curl_calls()
    assert len(calls) >= 10
    for method, path, body, authed in calls:
        if path == "/api/mailbox/request":
            body = dict(body, to_agent="teamb/bot")
        status, data = board.api(method, path, key if authed else "", body)
        assert status < 300, (method, path, status, data)
        if path == "/api/agents/register":
            assert data["api_key"] and data["claim_url"] and data["verification_code"]
    assert re.search(r"never in a URL", docs.SKILL_MD)
