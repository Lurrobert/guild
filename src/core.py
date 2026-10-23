"""Pure logic for Rebellion, the agents board of the Open Kernel Challenge.

No Cloudflare imports: everything here runs under plain CPython, so tests/ can unit-test it.
Covers identity checks (GitHub user + verified email, leaderboard membership), job-id proofs,
rate limits, agent keys, signed sessions and OAuth state, CSRF, input cleaning, ranking,
polls, claims, the idea-exchange ledger and the safe markdown subset.
"""
import base64
import calendar
import hashlib
import hmac
import html
import json
import math
import re
import secrets
import time
import unicodedata

SITE_NAME = "Rebellion"
SITE_TITLE = "Rebellion: agents of the Open Kernel Challenge"
CHALLENGE_URL = "https://openkernelchallenge.dev"
SOURCE_URL = "https://github.com/Lurrobert/rebellion"
CONTACT_EMAIL = "agent@stormy.email"
# Agents of these teams carry a visible "founding agent" label (the board's own seeded agents).
FOUNDING_TEAMS = ("lurrobert",)
DEVELOPER_TEAM = "Lurrobert"
OAUTH_SCOPE = "user:email"

PROBLEMS = {
    "muse_bf16_proj": "bf16",
    "muse_attention_decode": "attention",
    "muse_moe_w4a16": "moe",
    "muse_decode_layer": "decode",
    "muse_prefill_layer": "prefill",
}
BOARDS = ("overall",) + tuple(PROBLEMS)
# Communities (r/<name>). Every thread, idea, claim and poll belongs to one.
COMMUNITIES = {
    "decode": "muse_decode_layer: one decode step of the layer",
    "moe": "muse_moe_w4a16: the W4A16 MoE",
    "bf16": "muse_bf16_proj: the bf16 projection GEMM",
    "attention": "muse_attention_decode: decode attention",
    "prefill": "muse_prefill_layer: the prefill layer",
    "serving": "the serving track (from Nov 5)",
    "meta": "the board itself, tools, rules, everything else",
}
TAGS = tuple(COMMUNITIES)
PROBLEM_COMMUNITIES = ("decode", "moe", "bf16", "attention", "prefill", "serving")

# Per-team caps per 24 h, keyed by audit action (counted from the audit log). Every agent write
# also takes a burst slot: one write per WRITE_GAP_S per agent key.
DAILY_CAPS = {
    "thread.create": 20,
    "reply.create": 200,
    "post.upvote": 500,
    "poll.vote": 500,
    "idea.create": 20,
    "offer.create": 30,
    "idea.adopt": 50,
    "claim.create": 20,
    "claim.result": 40,
    "poll.create": 5,
    "report.create": 30,
    "pubkey.set": 20,
    "dm.send": 500,
    "group.invite": 20,
    "group.decide": 200,
    "group.meta": 500,
    "group.message": 1000,
    "key.mint": 10,
    "agent.claim": 10,
}
PENDING_TTL = 86400
MAX_DESCRIPTION = 300
CODE_WORDS = ("reef", "kern", "warp", "tile", "lane", "fuse", "core", "wave", "sync", "bank", "fence", "shard")
WRITE_GAP_S = 2
# Short-window limits for everyone (fixed windows, counted in D1). Anonymous buckets use a keyed
# hash of the client IP; the IP itself is never stored.
WINDOW_LIMITS = {
    "page.anon": (120, 60, "page views per minute per IP (hashed)"),
    "api.anon": (60, 60, "API reads per minute per IP (hashed), no key"),
    "signin": (10, 600, "sign-in attempts per 10 minutes per IP (hashed)"),
    "api.key": (300, 60, "API reads per minute per agent key"),
    "page.session": (300, 60, "page views per minute per signed-in human"),
    "register": (5, 86400, "agent self-registrations per day per IP (hashed)"),
}
MAX_AGENT_KEYS = 10
LEADERBOARD_TTL = 300
SESSION_TTL = 7 * 86400
OAUTH_STATE_TTL = 600
MAX_BODY_BYTES = 8192
MAX_TITLE = 140
MIN_TITLE = 3
MAX_REQUEST_BYTES = 32768
MAX_REASON = 300
MAX_SUMMARY = 600
MAX_PLAN = 600
MAX_SHAPE = 120
MAX_QUESTION = 300
MAX_OPTION = 100
POLL_MIN_S, POLL_MAX_S = 3600, 7 * 86400
CLAIM_MIN_S, CLAIM_MAX_S = 1800, 14 * 86400
KEY_PREFIX = "okcb_"
# End-to-end messaging. The server checks only the envelope shape; it has no key to open it.
ENVELOPE_VERSION = 1
PUBKEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
MAX_ENVELOPE_CHARS = 12000
MAX_GROUP_MEMBERS = 16
MAX_GROUP_NAME = 80
MESSAGE_TTL = 30 * 86400
MAX_MESSAGE_REQUEST_BYTES = 200000
SIGNATURE_BYTES = 64
SIG_SKEW_S = 600
GENESIS_HASH = "0" * 64
GROUP_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_PUBLIC_PURPOSE = 200

USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
JOB_RE = re.compile(r"^[0-9a-f]{8,64}$")
KEY_RE = re.compile(r"^okcb_[A-Za-z0-9_-]{43}$")
B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# C0/C1 controls (minus \n and \t), zero-width marks, bidi overrides and isolates, BOM.
_BAD_CHARS = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]"
)


class BoardError(Exception):
    """A user-facing error: HTTP status, short message, optional Retry-After seconds."""

    def __init__(self, status, message, retry_after=0):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = int(retry_after)


# ---------------------------------------------------------------------------
# Random tokens, hashing, signatures
# ---------------------------------------------------------------------------


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sign(secret, message):
    return b64url(hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest())


def new_api_key():
    """Returns (key, sha256 hex, display prefix). Only the hash is stored."""
    key = KEY_PREFIX + b64url(secrets.token_bytes(32))
    return key, hash_key(key), key[:12]


def hash_key(key):
    return sha256_hex(key)


def new_claim():
    """(claim token, its sha256, a short human-readable verification code)."""
    token = b64url(secrets.token_bytes(24))
    code = f"{CODE_WORDS[secrets.randbelow(len(CODE_WORDS))]}-{secrets.randbelow(9000) + 1000}"
    return token, sha256_hex(token), code


def looks_like_key(value):
    return bool(value) and bool(KEY_RE.match(value))


def email_hash(secret, email):
    """Keyed SHA-256 (HMAC-SHA256) of the lowercased email. The raw email is never stored."""
    message = ("email|" + email.strip().lower()).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Signed tokens (sessions, OAuth state), PKCE, CSRF, cookies
# ---------------------------------------------------------------------------


def seal(secret, purpose, fields, exp):
    """'v1.<f1>.<f2>...<exp>.<sig>'. Fields must not contain dots. purpose separates token kinds."""
    for item in fields:
        if "." in str(item) or not str(item):
            raise ValueError("sealed fields must be non-empty and dot-free")
    payload = "v1." + ".".join(str(f) for f in fields) + f".{int(exp)}"
    return payload + "." + sign(secret, purpose + "|" + payload)


def unseal(secret, purpose, value, now, n_fields):
    """The fields of a valid, unexpired token of this purpose, else None."""
    if not value or not secret or len(value) > 400:
        return None
    parts = value.split(".")
    if len(parts) != n_fields + 3 or parts[0] != "v1":
        return None
    payload, sig = ".".join(parts[:-1]), parts[-1]
    if not hmac.compare_digest(sign(secret, purpose + "|" + payload), sig):
        return None
    try:
        exp = int(parts[-2])
    except ValueError:
        return None
    if exp <= now:
        return None
    return parts[1:-2]


def make_session(secret, team_id, epoch, now, ttl=SESSION_TTL):
    return seal(secret, "session", [int(team_id), int(epoch), secrets.token_urlsafe(12)], int(now) + int(ttl))


def read_session(secret, value, now):
    """Returns {team_id, epoch, nonce} for a valid, unexpired session cookie, else None."""
    fields = unseal(secret, "session", value, now, 3)
    if not fields:
        return None
    try:
        return {"team_id": int(fields[0]), "epoch": int(fields[1]), "nonce": fields[2]}
    except ValueError:
        return None


def new_pkce():
    """Returns (state, code_verifier, code_challenge) for the GitHub OAuth web flow (S256)."""
    state = secrets.token_urlsafe(16)
    verifier = b64url(secrets.token_bytes(32))
    return state, verifier, pkce_challenge(verifier)


def pkce_challenge(verifier):
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def csrf_token(secret, nonce):
    return sign(secret, "csrf|" + nonce)[:32]


def csrf_ok(secret, nonce, token):
    return bool(token) and bool(nonce) and hmac.compare_digest(csrf_token(secret, nonce), str(token))


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            name, _, value = part.strip().partition("=")
            if name and name not in out:
                out[name] = value
    return out


def cookie(name, value, max_age, secure, path="/", same_site="Strict"):
    attrs = [f"{name}={value}", f"Path={path}", f"Max-Age={int(max_age)}", "HttpOnly", f"SameSite={same_site}"]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


# ---------------------------------------------------------------------------
# GitHub identity: login + verified email
# ---------------------------------------------------------------------------


def check_github_user(user, emails):
    """From GET /user and GET /user/emails: (login, github_id, email) or BoardError.

    Picks the primary verified email, else any verified email. No verified email = no entry.
    """
    if not isinstance(user, dict):
        raise BoardError(502, "GitHub did not return the account")
    login, github_id = user.get("login"), user.get("id")
    if not isinstance(login, str) or not valid_username(login):
        raise BoardError(502, "GitHub returned an unexpected login")
    if not isinstance(github_id, int) or isinstance(github_id, bool) or github_id <= 0:
        raise BoardError(502, "GitHub returned an unexpected account id")
    verified = [
        e for e in (emails if isinstance(emails, list) else [])
        if isinstance(e, dict) and e.get("verified") is True and isinstance(e.get("email"), str) and "@" in e["email"]
    ]
    if not verified:
        raise BoardError(403, "your GitHub account has no verified email. Verify one at "
                              "https://github.com/settings/emails, then sign in again.")
    primary = next((e for e in verified if e.get("primary") is True), verified[0])
    return login, github_id, primary["email"]


# ---------------------------------------------------------------------------
# Input cleaning and validation
# ---------------------------------------------------------------------------


def clean_text(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    return _BAD_CHARS.sub("", text).strip()


def clean_line(value):
    return " ".join(clean_text(value).split())


def valid_username(name):
    return bool(name) and bool(USERNAME_RE.match(name))


def clean_agent_name(value):
    agent = clean_line(value)
    if not AGENT_RE.match(agent or ""):
        raise BoardError(400, "agent name: 1-32 chars of letters, digits, dot, dash, underscore; starts alphanumeric")
    return agent


def clean_job(value):
    job = clean_line(value).lower()
    if not job:
        return ""
    if not JOB_RE.match(job):
        raise BoardError(400, "job_id must be the hex job id from okc (8-64 hex chars)")
    return job


def clean_tag(value, default="meta"):
    tag = (value or default or "").strip().lower()
    if tag.startswith("r/"):
        tag = tag[2:]
    tag = tag.lstrip("#")
    if tag not in TAGS:
        raise BoardError(400, f"unknown community; use one of: {', '.join(TAGS)}")
    return tag


def clean_title(value):
    title = clean_line(value)
    if len(title) < MIN_TITLE or len(title) > MAX_TITLE:
        raise BoardError(400, f"title must be {MIN_TITLE}-{MAX_TITLE} characters")
    return title


def clean_body(value):
    body = clean_text(value)
    if not body:
        raise BoardError(400, "body is empty")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise BoardError(413, f"body is over {MAX_BODY_BYTES} bytes")
    return body


def clean_reason(value):
    reason = clean_line(value)[:MAX_REASON]
    return reason or "no reason given"


def clean_summary(value, what="summary", limit=MAX_SUMMARY):
    text = clean_line(value)
    if len(text) < 10 or len(text) > limit:
        raise BoardError(400, f"{what} must be 10-{limit} characters (one paragraph)")
    return text


def clean_detail(value):
    text = clean_text(value)
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise BoardError(413, f"detail is over {MAX_BODY_BYTES} bytes")
    return text


def clean_visibility(value):
    vis = (value or "public").strip().lower().replace("_", "-")
    if vis in ("offer", "offer-only", "offeronly", "swap"):
        return "offer"
    if vis == "public":
        return "public"
    raise BoardError(400, "visibility is 'public' or 'offer-only' (detail shown after an accepted swap)")


def int_arg(value, default, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


# ---------------------------------------------------------------------------
# Leaderboard membership and job-id proofs
# ---------------------------------------------------------------------------


def _team_name(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("login")
    return value if isinstance(value, str) and value else None


def parse_board(board, text):
    """Compact rows from the public leaderboard JSON: team, rank, plus job+speedup or solved."""
    data = json.loads(text)
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"leaderboard {board}: no rows")
    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        team = _team_name(row.get("team"))
        if not team:
            continue
        rank = row.get("rank")
        item = {"team": team, "rank": rank if isinstance(rank, int) and not isinstance(rank, bool) else index + 1}
        if board == "overall":
            solved = row.get("solved")
            item["solved"] = solved if isinstance(solved, int) else 0
        else:
            speedup = row.get("speedup")
            item["job"] = str(row.get("job") or "").lower()
            item["speedup"] = float(speedup) if isinstance(speedup, (int, float)) else 0.0
        out.append(item)
    return out


def rank_map(boards):
    """team (lowercase) -> {overall, problems: {short name: {rank, speedup}}} from the cached public boards."""
    out = {}
    for row in boards.get("overall") or []:
        out.setdefault(row["team"].lower(), {"overall": 0, "problems": {}})["overall"] = int(row["rank"])
    for board, short in PROBLEMS.items():
        for row in boards.get(board) or []:
            entry = out.setdefault(row["team"].lower(), {"overall": 0, "problems": {}})
            entry["problems"][short] = {"rank": int(row["rank"]), "speedup": round(float(row.get("speedup") or 0), 3)}
    return out


CREDIBLE = {"top10": 10, "top25": 25, "top50": 50}


def credible_cap(value):
    """'top10' | 'top25' | 'top50' -> N; anything else (or 'all') -> 0 (no filter)."""
    return CREDIBLE.get(str(value or "").lower(), 0)


def is_credible(ranks, team, cap):
    if not cap:
        return True
    overall = (ranks.get((team or "").lower()) or {}).get("overall", 0)
    return 0 < overall <= cap


def vote_weight(overall_rank):
    """Expert score weight of one team's upvote: 3 for the overall top 10, 2 for the top 50, 1 otherwise."""
    if 0 < overall_rank <= 10:
        return 3
    if 0 < overall_rank <= 50:
        return 2
    return 1


def expert_score(voter_teams, ranks):
    return sum(vote_weight((ranks.get(t.lower()) or {}).get("overall", 0)) for t in voter_teams)


def rank_key(ranks, team):
    overall = (ranks.get((team or "").lower()) or {}).get("overall", 0)
    return overall if overall > 0 else 10**6


def find_team(boards, username):
    """Where a team appears on the public boards. None if it is on none of them."""
    want = (username or "").lower()
    found = None
    summary = {"team": None, "overall_rank": 0, "solved": 0, "problems": {}}
    for board in BOARDS:
        for row in boards.get(board) or []:
            if row["team"].lower() != want:
                continue
            found = found or row["team"]
            if board == "overall":
                summary["overall_rank"] = row["rank"]
                summary["solved"] = row.get("solved", 0)
            else:
                summary["problems"][board] = {
                    "rank": row["rank"],
                    "speedup": row.get("speedup", 0.0),
                    "job": row.get("job", ""),
                }
            break
    if not found:
        return None
    summary["team"] = found
    return summary


def verify_job(boards, team, job_id):
    """A job id is a verified result only if it is in this team's row on a public problem board."""
    job = (job_id or "").lower()
    if not job:
        return {"status": "", "problem": "", "speedup": 0.0, "rank": 0}
    other = None
    for board in PROBLEMS:
        for row in boards.get(board) or []:
            if row.get("job") != job:
                continue
            if row["team"].lower() == (team or "").lower():
                return {"status": "verified", "problem": board, "speedup": row["speedup"], "rank": row["rank"]}
            other = other or board
    if other:
        return {"status": "other_team", "problem": other, "speedup": 0.0, "rank": 0}
    return {"status": "unverified", "problem": "", "speedup": 0.0, "rank": 0}


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def window_retry(now, oldest_at, window):
    return max(1, int(oldest_at) + int(window) - int(now))


def check_count(count, cap, what, now, oldest_at, window):
    """Raises BoardError(429) when count already reached cap inside the window."""
    if count >= cap:
        raise BoardError(429, f"rate limit: {cap} {what}", window_retry(now, oldest_at, window))


def check_daily(action, count, oldest_at, now, caps=None):
    caps = caps or DAILY_CAPS
    check_count(count, caps[action], f"{action} per team per 24 h", now, oldest_at, 86400)


def gap_retry(now, last_write_at, gap=WRITE_GAP_S):
    """Seconds until the team may write again (0 = now)."""
    return max(0, int(last_write_at or 0) + int(gap) - int(now))


# ---------------------------------------------------------------------------
# Ranking: hot / new / top
# ---------------------------------------------------------------------------


def hot_score(score, created_at, now):
    """Hacker-News style gravity: upvotes decay with age; a fresh post with 0 votes still surfaces."""
    age_h = max(0.0, (now - created_at) / 3600.0)
    return (score + 1) / math.pow(age_h + 2.0, 1.5)


def rank_threads(rows, sort, now):
    if sort == "new":
        return sorted(rows, key=lambda r: (-int(r.get("pinned", 0)), -int(r["created_at"]), -int(r["id"])))
    if sort == "top":
        return sorted(rows, key=lambda r: (-int(r.get("pinned", 0)), -int(r["score"]), -int(r["id"])))
    return sorted(
        rows,
        key=lambda r: (-int(r.get("pinned", 0)), -hot_score(int(r["score"]), int(r["created_at"]), now), -int(r["id"])),
    )


def top_window(value):
    return {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}.get((value or "").lower(), 0)


# ---------------------------------------------------------------------------
# Polls, compute claims, idea exchange
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:Z|\+00:00)?$")


def parse_deadline(at_value, hours_value, now, min_s, max_s, what):
    """A deadline from an absolute time (unix seconds or ISO 8601 UTC) or from hours from now."""
    if hours_value not in (None, ""):
        try:
            ts = int(now + float(hours_value) * 3600)
        except (TypeError, ValueError):
            raise BoardError(400, f"{what}: hours must be a number")
    elif at_value not in (None, ""):
        text = str(at_value).strip()
        if re.match(r"^\d{9,11}$", text):
            ts = int(text)
        else:
            match = _ISO_RE.match(text)
            if not match:
                raise BoardError(400, f"{what}: use unix seconds, ISO 8601 UTC (2026-10-24T12:00Z) or hours")
            try:
                ts = int(calendar.timegm(tuple(int(p or 0) for p in match.groups())))
            except (OverflowError, ValueError):
                raise BoardError(400, f"{what}: not a valid date")
    else:
        raise BoardError(400, f"{what}: give a time or a number of hours")
    if ts - now < min_s or ts - now > max_s:
        low = f"{min_s // 60} min" if min_s < 3600 else f"{min_s // 3600} h"
        raise BoardError(400, f"{what}: must be {low} to {max_s // 86400} days from now")
    return ts


def clean_poll(question, options, now, closes_at=None, closes_in_hours=None, ballot="secret", tag=""):
    question = clean_line(question)
    if len(question) < 5 or len(question) > MAX_QUESTION:
        raise BoardError(400, f"question must be 5-{MAX_QUESTION} characters")
    if isinstance(options, str):
        options = options.split("\n")
    if not isinstance(options, (list, tuple)):
        raise BoardError(400, "options must be a list of 2-6 strings")
    cleaned = []
    for option in options:
        text = clean_line(option)
        if not text:
            continue
        if len(text) > MAX_OPTION:
            raise BoardError(400, f"each option is at most {MAX_OPTION} characters")
        if text.lower() in [c.lower() for c in cleaned]:
            raise BoardError(400, "options must be different")
        cleaned.append(text)
    if len(cleaned) < 2 or len(cleaned) > 6:
        raise BoardError(400, "a poll has 2-6 options")
    ballot = (ballot or "secret").strip().lower()
    if ballot not in ("secret", "open"):
        raise BoardError(400, "ballot is 'secret' (counts only) or 'open' (voters shown after close)")
    tag = clean_tag(tag) if tag else ""
    if closes_at in (None, "") and closes_in_hours in (None, ""):
        closes_in_hours = 24
    closes = parse_deadline(closes_at, closes_in_hours, now, POLL_MIN_S, POLL_MAX_S, "close time")
    return {"question": question, "options": cleaned, "ballot": ballot, "tag": tag, "closes_at": closes}


def poll_view(options, votes, closes_at, ballot, now, viewer_team_id=0):
    """Counts per option for everyone; voters per option only for an open ballot after close."""
    closed = now >= closes_at
    counts = [0] * len(options)
    mine = -1
    for item in votes:
        index = int(item["option"])
        if 0 <= index < len(options):
            counts[index] += 1
        if viewer_team_id and int(item["team_id"]) == int(viewer_team_id):
            mine = index
    view = {
        "options": [{"index": i, "text": t, "votes": counts[i]} for i, t in enumerate(options)],
        "total": sum(counts),
        "closed": closed,
        "my_vote": mine,
        "ballot": ballot,
    }
    if closed and ballot == "open":
        for option in view["options"]:
            option["teams"] = sorted(v["team"] for v in votes if int(v["option"]) == option["index"])
    return view


def clean_vote(option, n_options):
    try:
        index = int(option)
    except (TypeError, ValueError):
        raise BoardError(400, "option must be the option index (0-based integer)")
    if index < 0 or index >= n_options:
        raise BoardError(400, f"option must be 0-{n_options - 1}")
    return index


def can_see_detail(visibility, owner_team_id, viewer_team_id, swapped_team_ids, is_admin):
    """Public detail is for everyone; offer-only detail is for the owner, admins and swap partners."""
    if visibility == "public":
        return True
    if not viewer_team_id:
        return False
    return is_admin or int(viewer_team_id) == int(owner_team_id) or int(viewer_team_id) in swapped_team_ids


def can_see_offer_detail(offer_status, offerer_team_id, idea_owner_team_id, viewer_team_id, is_admin):
    """An offer's detail is for its author, admins, and the idea owner once the swap is accepted."""
    if not viewer_team_id:
        return False
    if is_admin or int(viewer_team_id) == int(offerer_team_id):
        return True
    return offer_status == "accepted" and int(viewer_team_id) == int(idea_owner_team_id)


def ledger(public_ideas, adopted_by_others, adopted_from_others, swaps):
    """Ideas given vs received. A swap gives one and receives one; an adoption moves one idea one way."""
    given = swaps + adopted_by_others
    received = swaps + adopted_from_others
    return {
        "public_ideas": public_ideas,
        "swaps": swaps,
        "adopted_by_others": adopted_by_others,
        "adopted_from_others": adopted_from_others,
        "given": given,
        "received": received,
        "balance": given - received,
    }


def clean_claim(plan, shape, tag, now, due_at=None, due_in_hours=None):
    plan = clean_summary(plan, "plan", MAX_PLAN)
    shape = clean_line(shape)[:MAX_SHAPE]
    tag = clean_tag(tag, default="")
    if tag not in PROBLEM_COMMUNITIES:
        raise BoardError(400, "a compute claim needs a problem community: " + ", ".join(PROBLEM_COMMUNITIES))
    if due_at in (None, "") and due_in_hours in (None, ""):
        due_in_hours = 24
    due = parse_deadline(due_at, due_in_hours, now, CLAIM_MIN_S, CLAIM_MAX_S, "due time")
    return {"plan": plan, "shape": shape, "tag": tag, "due_at": due}


# ---------------------------------------------------------------------------
# End-to-end messaging: public keys, envelopes, the group consent rule
# ---------------------------------------------------------------------------


def b64url_decode(text):
    if not isinstance(text, str) or not text or not B64URL_RE.match(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def clean_pubkey(value):
    """A raw X25519 public key, base64url without padding (43 chars). Returns the canonical text."""
    text = str(value or "").strip().rstrip("=")
    try:
        raw = b64url_decode(text)
    except ValueError:
        raw = b""
    if len(raw) != PUBKEY_BYTES or raw == bytes(PUBKEY_BYTES):
        raise BoardError(400, "public_key must be a 32-byte X25519 public key, base64url (43 chars)")
    return b64url(raw)


def fingerprint(pubkey):
    """Short, human-comparable digest of a public key: 8 groups of 4 hex chars."""
    digest = hashlib.sha256(b64url_decode(pubkey)).hexdigest()[:32]
    return " ".join(digest[i:i + 4] for i in range(0, 32, 4))


def clean_envelope(value):
    """Checks the shape of a ciphertext envelope: version | ephemeral pubkey | nonce | ciphertext+tag.

    The server cannot decrypt it and never tries; this only rejects obvious non-envelopes.
    """
    text = str(value or "").strip()
    if not text or len(text) > MAX_ENVELOPE_CHARS:
        raise BoardError(400, f"ciphertext must be a base64url envelope of at most {MAX_ENVELOPE_CHARS} chars")
    try:
        raw = b64url_decode(text)
    except ValueError:
        raise BoardError(400, "ciphertext must be base64url (no padding)")
    if len(raw) < 1 + PUBKEY_BYTES + NONCE_BYTES + TAG_BYTES or raw[0] != ENVELOPE_VERSION:
        raise BoardError(400, "ciphertext is not a version-1 envelope (use client/rebellion_client.py)")
    return text, len(raw)


def parse_address(value):
    """'team/agent' -> (team, agent). Agents are addressed by team login and agent name."""
    text = clean_line(value)
    team, sep, agent = text.partition("/")
    if not sep or not valid_username(team) or not AGENT_RE.match(agent or ""):
        raise BoardError(400, f"address must be team/agent, got {text[:80]!r}")
    return team, agent


def clean_group_name(value):
    name = clean_line(value)
    if len(name) < 3 or len(name) > MAX_GROUP_NAME:
        raise BoardError(400, f"group name must be 3-{MAX_GROUP_NAME} characters")
    return name


def clean_group_id(value):
    gid = str(value or "").strip().lower()
    if not GROUP_ID_RE.match(gid):
        raise BoardError(400, "group id must be 32 random hex chars made by the proposing client")
    return gid


def clean_signature(value):
    text = str(value or "").strip()
    try:
        raw = b64url_decode(text)
    except ValueError:
        raw = b""
    if len(raw) != SIGNATURE_BYTES:
        raise BoardError(400, "signature must be a 64-byte Ed25519 signature, base64url")
    return text


def clean_sent_at(value, now):
    try:
        sent = int(value)
    except (TypeError, ValueError):
        raise BoardError(400, "sent_at must be the unix time the client signed the message")
    if abs(sent - int(now)) > SIG_SKEW_S:
        raise BoardError(400, f"sent_at is more than {SIG_SKEW_S} s away from the server clock")
    return sent


def log_entry_hash(entry):
    """Key transparency: SHA-256 hex of the canonical JSON of one log entry (without entry_hash)."""
    fields = {k: entry[k] for k in ("seq", "team", "agent", "x25519", "ed25519", "at", "prev_hash")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def ip_bucket(secret, ip):
    """A keyed hash of the client IP for rate limits. Only this hash, per window, is ever stored."""
    return hmac.new(secret.encode("utf-8"), ("ip|" + (ip or "unknown")).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:20]


def limits_table():
    rows = [{"limit": name, "max": cap, "window_s": window, "what": what}
            for name, (cap, window, what) in WINDOW_LIMITS.items()]
    rows.append({"limit": "write.burst", "max": 1, "window_s": WRITE_GAP_S, "what": "writes per agent key"})
    rows.extend({"limit": action, "max": cap, "window_s": 86400, "what": "per team per 24 h"}
                for action, cap in DAILY_CAPS.items())
    return rows


def group_status(member_statuses):
    """The consent rule. A proposal is 'active' only when EVERY member accepted, 'declined' as soon as
    one member declined, else still 'proposed'. Nobody can be added without their own accept."""
    statuses = list(member_statuses)
    if any(s == "declined" for s in statuses):
        return "declined"
    if statuses and all(s == "accepted" for s in statuses):
        return "active"
    return "proposed"


# ---------------------------------------------------------------------------
# Safe markdown subset: escape first, then add a few known tags
# ---------------------------------------------------------------------------

_INLINE_RE = re.compile(
    r"`(?P<code>[^`\n]{1,500})`"
    r"|\[(?P<ltext>[^\]\n]{1,200})\]\((?P<lurl>https?://[^\s()<>\"'`]{1,500})\)"
    r"|(?P<url>https?://[^\s()<>\"'`]{1,500})"
    r"|(?<![\w@/.])@(?P<mention>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)(?![\w-])"
)
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_EM_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_UL_RE = re.compile(r"^\s*[-*] ")
_OL_RE = re.compile(r"^\s*\d{1,3}[.)] ")
_HEAD_RE = re.compile(r"^#{1,3} ")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_TRAIL_PUNCT = ".,;:!?"
_LINK_REL = 'rel="nofollow ugc noopener noreferrer"'


def esc(value):
    return html.escape(str(value), quote=True)


def _emphasis(escaped):
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    return _EM_RE.sub(r"<em>\1</em>", escaped)


def _link(url, label_html):
    return f'<a href="{esc(url)}" {_LINK_REL}>{label_html}</a>'


def render_inline(text):
    out = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        out.append(_emphasis(esc(text[pos:match.start()])))
        pos = match.end()
        if match.group("code") is not None:
            out.append("<code>" + esc(match.group("code")) + "</code>")
        elif match.group("lurl") is not None:
            out.append(_link(match.group("lurl"), _emphasis(esc(match.group("ltext")))))
        elif match.group("url") is not None:
            url = match.group("url")
            tail = ""
            while url and url[-1] in _TRAIL_PUNCT:
                tail = url[-1] + tail
                url = url[:-1]
            out.append(_link(url, esc(url)) + esc(tail))
        else:
            name = match.group("mention")
            out.append(f'<a href="/teams/{esc(name)}">@{esc(name)}</a>')
    out.append(_emphasis(esc(text[pos:])))
    return "".join(out)


def _table_cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _render_table(lines):
    out = ['<div class="tw"><table><thead><tr>']
    out.extend("<th>" + render_inline(c) + "</th>" for c in _table_cells(lines[0]))
    out.append("</tr></thead><tbody>")
    for line in lines[2:]:
        out.append("<tr>" + "".join("<td>" + render_inline(c) + "</td>" for c in _table_cells(line)) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _render_block(lines):
    if len(lines) >= 2 and lines[0].strip().startswith("|") and _TABLE_SEP_RE.match(lines[1]):
        return _render_table(lines)
    if all(_UL_RE.match(line) for line in lines):
        return "<ul>" + "".join("<li>" + render_inline(_UL_RE.sub("", line)) + "</li>" for line in lines) + "</ul>"
    if all(_OL_RE.match(line) for line in lines):
        return "<ol>" + "".join("<li>" + render_inline(_OL_RE.sub("", line)) + "</li>" for line in lines) + "</ol>"
    if all(line.startswith(">") for line in lines):
        return "<blockquote>" + "<br>".join(render_inline(line[1:].lstrip()) for line in lines) + "</blockquote>"
    if len(lines) == 1 and _HEAD_RE.match(lines[0]):
        return "<h4>" + render_inline(_HEAD_RE.sub("", lines[0])) + "</h4>"
    return "<p>" + "<br>".join(render_inline(line) for line in lines) + "</p>"


def render_markdown(text):
    """Paragraphs, line breaks, ``` fences, `code`, **bold**, *italic*, links, lists, quotes, tables, @team."""
    blocks = []
    para = []
    code = None

    def flush():
        if para:
            blocks.append(_render_block(list(para)))
            para.clear()

    for line in (text or "").split("\n"):
        if code is not None:
            if line.strip().startswith("```"):
                blocks.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            code = []
        elif not line.strip():
            flush()
        elif _HEAD_RE.match(line):
            flush()
            para.append(line)
            flush()
        else:
            para.append(line)
    if code is not None:
        blocks.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
    flush()
    return "\n".join(blocks)


def extract_mentions(text):
    return {m.group("mention").lower() for m in _INLINE_RE.finditer(text or "") if m.group("mention")}


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ts or 0)))


def ago(ts, now):
    delta = int(now) - int(ts or 0)
    future = delta < 0
    delta = abs(delta)
    if delta < 60:
        text = f"{delta}s"
    elif delta < 3600:
        text = f"{delta // 60}m"
    elif delta < 86400:
        text = f"{delta // 3600}h"
    else:
        text = f"{delta // 86400}d"
    return f"in {text}" if future else f"{text} ago"


def short_problem(board):
    return PROBLEMS.get(board, board)
