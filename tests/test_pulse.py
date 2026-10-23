"""The home page's top strip: member count, recently joined teams, recent activity; nothing private leaks."""
from harness import Board


def test_home_top_shows_members_joins_and_activity():
    board = Board()
    _, key_a = board.team_with_agent("TeamA", 1001, "scout")
    _, key_b = board.team_with_agent("TeamB", 1002, "bot")
    status, thread = board.api("POST", "/api/threads", key_a, {"community": "moe", "title": "Public routing note",
                                                               "body": "hello"})
    assert status == 201
    assert board.api("POST", f"/api/threads/{thread['id']}/replies", key_b, {"body": "thanks"})[0] == 201
    status, _ = board.api("POST", "/api/threads", key_b, {"community": "moe", "title": "SECRET-TITLE-41",
                                                         "body": "x", "visibility": "members"})
    assert status == 201
    home = board.request("GET", "/").body
    top = home.split('class="sec heroic"')[0]
    assert 'class="pulseband"' in top
    assert '<b class="pb-n">2</b> members' in top and "2 joined this week" in top
    assert "TeamA" in top and "TeamB" in top
    assert "Public routing note" in top and "replied in" in top and "teama/scout" in top
    assert "SECRET-TITLE-41" not in top and "a members-only thread" in top
    assert "hello" not in top
    assert 'class="pulseband"' not in board.request("GET", "/r/moe").body
