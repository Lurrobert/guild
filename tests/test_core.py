import json

import pytest

import core
from core import BoardError

SECRET = "k" * 64


def test_api_key_is_random_and_only_hash_kept():
    key, digest, prefix = core.new_api_key()
    key2, _, _ = core.new_api_key()
    assert key != key2 and core.looks_like_key(key)
    assert digest == core.hash_key(key) and len(digest) == 64 and key not in digest
    assert prefix == key[:12] and len(key) == 48


def test_email_hash_is_keyed_and_normalized():
    a = core.email_hash(SECRET, "Dev@Example.com ")
    assert a == core.email_hash(SECRET, "dev@example.com")
    assert a != core.email_hash("other" * 10, "dev@example.com")
    assert "example" not in a


def test_seal_rejects_tamper_expiry_and_purpose():
    token = core.seal(SECRET, "oauth", ["abc", "def"], 200)
    assert core.unseal(SECRET, "oauth", token, 100, 2) == ["abc", "def"]
    assert core.unseal(SECRET, "oauth", token, 200, 2) is None
    assert core.unseal(SECRET, "session", token, 100, 2) is None
    assert core.unseal("x" * 64, "oauth", token, 100, 2) is None
    assert core.unseal(SECRET, "oauth", token.replace("abc", "abd"), 100, 2) is None


def test_session_and_csrf():
    cookie = core.make_session(SECRET, 7, 2, 1000)
    session = core.read_session(SECRET, cookie, 1001)
    assert session["team_id"] == 7 and session["epoch"] == 2
    token = core.csrf_token(SECRET, session["nonce"])
    assert core.csrf_ok(SECRET, session["nonce"], token)
    assert not core.csrf_ok(SECRET, session["nonce"], "")
    assert not core.csrf_ok(SECRET, "othernonce", token)
    assert core.read_session(SECRET, cookie, 1000 + core.SESSION_TTL + 1) is None


def test_pkce_challenge_is_s256():
    state, verifier, challenge = core.new_pkce()
    assert challenge == core.pkce_challenge(verifier) and len(challenge) == 43 and state


def test_github_user_needs_verified_email():
    user = {"login": "TeamA", "id": 42}
    emails = [{"email": "a@x.dev", "verified": False, "primary": True},
              {"email": "b@x.dev", "verified": True, "primary": False}]
    assert core.check_github_user(user, emails) == ("TeamA", 42, "b@x.dev")
    primary = emails + [{"email": "c@x.dev", "verified": True, "primary": True}]
    assert core.check_github_user(user, primary)[2] == "c@x.dev"
    with pytest.raises(BoardError) as err:
        core.check_github_user(user, [{"email": "a@x.dev", "verified": False}])
    assert err.value.status == 403
    with pytest.raises(BoardError):
        core.check_github_user({"login": "bad login", "id": 1}, primary)
    with pytest.raises(BoardError):
        core.check_github_user({"login": "ok", "id": True}, primary)


def test_leaderboard_parse_find_and_job_proof():
    overall = json.dumps({"rows": [{"rank": 1, "team": "Alpha", "solved": 4}, {"rank": 2, "team": "beta", "solved": 3}]})
    moe = json.dumps({"rows": [{"job": "ABCDEF0123", "team": "beta", "speedup": 1.9}, {"job": "1111aaaa", "team": "Alpha",
                                                                                     "speedup": 2.3}]})
    boards = {"overall": core.parse_board("overall", overall), "muse_moe_w4a16": core.parse_board("muse_moe_w4a16", moe)}
    found = core.find_team(boards, "BETA")
    assert found["team"] == "beta" and found["overall_rank"] == 2
    assert found["problems"]["muse_moe_w4a16"]["rank"] == 1
    assert core.find_team(boards, "gamma") is None
    assert core.verify_job(boards, "beta", "abcdef0123")["status"] == "verified"
    assert core.verify_job(boards, "Alpha", "abcdef0123")["status"] == "other_team"
    assert core.verify_job(boards, "Alpha", "deadbeef")["status"] == "unverified"
    with pytest.raises(ValueError):
        core.parse_board("overall", "{}")


def test_rate_limits():
    core.check_daily("thread.create", 19, 0, 100)
    with pytest.raises(BoardError) as err:
        core.check_daily("thread.create", 20, 50, 100)
    assert err.value.status == 429 and err.value.retry_after == 50 + 86400 - 100
    assert core.gap_retry(100, 99) == 1 and core.gap_retry(100, 80) == 0


@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "[click](javascript:alert(1))",
    '[x](https://ok.dev/"onmouseover="alert(1))',
    "**<b>bold</b>**",
    "```\n</code></pre><script>x</script>\n```",
    "| a | b |\n|---|---|\n| <i>c</i> | d |",
])
def test_markdown_escapes_everything(payload):
    out = core.render_markdown(payload)
    assert "<script" not in out and "<img" not in out and "<i>" not in out and "<b>" not in out
    assert 'href="javascript' not in out
    assert 'onmouseover="' not in out and 'onerror="' not in out


def test_markdown_subset_works():
    out = core.render_markdown("**hi** `x<y` https://a.dev/p. @TeamA\n\n- one\n- two")
    assert "<strong>hi</strong>" in out and "<code>x&lt;y</code>" in out
    assert '<a href="https://a.dev/p" rel="nofollow ugc noopener noreferrer">' in out
    assert '<a href="/teams/TeamA">@TeamA</a>' in out and "<ul><li>one</li>" in out


def test_clean_text_strips_controls_and_bidi():
    assert core.clean_text("a‮b​c\x00d") == "abcd"


def test_group_consent_rule():
    assert core.group_status(["accepted", "invited", "invited"]) == "proposed"
    assert core.group_status(["accepted", "accepted", "invited"]) == "proposed"
    assert core.group_status(["accepted", "accepted", "accepted"]) == "active"
    assert core.group_status(["accepted", "declined", "accepted"]) == "declined"
    assert core.group_status([]) == "proposed"


def test_pubkey_and_envelope_shapes():
    raw = bytes(range(1, 33))
    pub = core.b64url(raw)
    assert core.clean_pubkey(pub + "=") == pub
    assert len(core.fingerprint(pub).split()) == 8
    for bad in ("", "abc", core.b64url(bytes(32)), core.b64url(raw + b"x"), pub[:-1] + "!"):
        with pytest.raises(BoardError):
            core.clean_pubkey(bad)
    envelope = core.b64url(bytes([1]) + bytes(32) + bytes(12) + b"c" * 20)
    assert core.clean_envelope(envelope) == (envelope, 65)
    for bad in ("hello world", core.b64url(b"plaintext message here " * 3),
                core.b64url(bytes([2]) + bytes(70)), "A" * (core.MAX_ENVELOPE_CHARS + 1)):
        with pytest.raises(BoardError):
            core.clean_envelope(bad)


def test_address_parsing():
    assert core.parse_address(" TeamA/kernel-scout ") == ("TeamA", "kernel-scout")
    for bad in ("TeamA", "/x", "Team A/x", "TeamA/-x", "a/" + "x" * 40):
        with pytest.raises(BoardError):
            core.parse_address(bad)
