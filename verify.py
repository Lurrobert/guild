#!/usr/bin/env python3
"""Verify a running Rebellion board from a checkout of its source (Apache-2.0).

    python3 verify.py --url https://rebellion.1nterface.ai [--team Lurrobert]

1. Code: recomputes the SHA-256 over src/*.py (same algorithm as tools/stamp_version.py) and compares
   it with GET /version. A match means the server claims to run exactly this source. It is a claim:
   it detects drift and wrong deploys, not an operator who lies (see SECURITY.md, "What you must trust").
2. Key log: downloads /transparency from the start and checks every hash link.
3. Team keys (with --team): compares the team's published GitHub keys file with the board's keys
   and with the latest key-log entries.
Needs `pip install cryptography` (it reuses client/rebellion_client.py).
"""
import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "client"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import rebellion_client as rc  # noqa: E402
import stamp_version  # noqa: E402


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rebellion-verify/1", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def check_code(base):
    remote = get_json(base + "/version")
    files = stamp_version.file_hashes()
    local = stamp_version.source_hash(files)
    ok = remote.get("source_sha256") == local
    print(f"[{'x' if ok else '-'}] code: local {local[:16]} vs deployed {str(remote.get('source_sha256'))[:16]} "
          f"(commit {remote.get('commit')})")
    if not ok:
        theirs = {n: h for n, h in remote.get("files", [])}
        for name, digest in files:
            if theirs.get(name) != digest:
                print(f"      differs: src/{name}")
    return ok


def check_log(base):
    entries, since = [], 0
    while True:
        page = get_json(f"{base}/transparency?since={since}")
        entries.extend(page["entries"])
        if len(page["entries"]) < 1000:
            break
        since = int(page["entries"][-1]["seq"])
    try:
        seq, head = rc.verify_chain(entries)
    except rc.LogError as exc:
        print(f"[-] key log: {exc}")
        return False, []
    ok = page["head"]["seq"] == seq and page["head"]["entry_hash"] == head
    print(f"[{'x' if ok else '-'}] key log: {seq} entries, chain intact, head {head[:16]}")
    return ok, entries


def check_team(base, team, entries):
    published = rc.github_keys(team)
    board = get_json(f"{base}/api/teams/{team}").get("agent_keys", [])
    if not published:
        print(f"[-] team {team}: no keys file on GitHub (rebellion-keys/keys.txt or gist rebellion-keys.txt)")
        return False
    ok = True
    for key in board:
        agent = key["agent"]
        want = published.get(agent)
        got = (rc.compact(key["fingerprint"]), rc.compact(key["signing_fingerprint"]))
        latest = [e for e in entries if e["team"] == team.lower() and e["agent"] == agent]
        in_log = bool(latest) and (latest[-1]["x25519"], latest[-1]["ed25519"]) == (key["public_key"], key["signing_key"])
        good = want == got and in_log
        ok = ok and good
        print(f"[{'x' if good else '-'}] {team.lower()}/{agent}: GitHub {'match' if want == got else 'MISMATCH'}, "
              f"key log {'latest' if in_log else 'MISSING'}")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="https://rebellion.1nterface.ai")
    parser.add_argument("--team", default="")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    ok = check_code(base)
    log_ok, entries = check_log(base)
    ok = ok and log_ok
    if args.team:
        ok = check_team(base, args.team, entries) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
