# Rebellion board: status (2026-10-23, work stopped by owner: out of compute)

Nothing deployed. No D1 database created. No secrets set. No git.

## What exists (board/)
| file | state |
|---|---|
| schema.sql | done. D1/SQLite, idempotent. Applies cleanly to sqlite3 (checked). Tables: teams, api_keys (named agent keys), posts (threads+replies, community, score), post_votes, ideas, offers, adoptions, claims, polls, votes, reports, audit_log, lb_cache. No IPs, no tokens, no raw emails. |
| src/core.py | done. Pure logic: OAuth state seal/unseal + PKCE, sessions, CSRF, key gen/hash, HMAC email hash, GitHub user+verified-email check, leaderboard parse / team lookup / job-id proof, rate limits, hot/new/top, polls, claims, idea ledger, safe markdown (escape first). |
| src/store.py | done (untested). All data ops on an async db (D1 or sqlite): OAuth-only login (scope user:email, /user + /user/emails, token dropped), ranked-team gate (leaderboard cached 5 min), github_id pinning, named agent keys (human mints/rotates/revokes), threads/replies/upvotes/karma, idea exchange, claims, polls, reports + auto-hide, admin hide/pin/lock/suspend, audit log, team + agent profiles. |
| src/views.py | done (untested). HTML, monochrome, dark mode, no JS, CSP hash for the inline style. |
| src/app.py | done (untested). Router with 80 routes: HTML pages + forms (CSRF + Origin check) and the JSON API (Bearer keys only). |
| src/docs.py | STUB. Placeholder texts. |

Check run: `python3 -c "import core, store, views, docs, app"` in src/ passes (80 routes). No unit tests were run: none are written yet.

## Not done
- src/entry.py (Cloudflare Python Worker adapter: D1 wrapper with to_py(), fetch-based http, Config from env per request, DEV_* overrides for localhost only). Design notes are in the session; the workers SDK API was checked (Request.headers is HTTPMessage, Response(body, status, headers=list of tuples), no body for 204).
- wrangler.toml (name okc-agents-board or okc-rebellion, compatibility_flags = ["python_workers"], D1 binding DB, vars GITHUB_CLIENT_ID = "Ov23liq0WDvFZO8QgZ66", ADMIN_TEAMS = "Lurrobert", PUBLIC_ORIGIN = "https://rebellion.1nterface.ai", routes = [{ pattern = "rebellion.1nterface.ai", custom_domain = true }]).
- prompts/AGENTS.md, SECURITY.md, RULES.md, then generate src/docs.py from them (plus a test that keeps them in sync).
- tests/ (sqlite fake db + fake http for leaderboard/GitHub; unit tests for core; flow tests for app).
- README.md, LICENSE (Apache-2.0), .gitignore (.wrangler/, .dev.vars, node_modules).
- Local `npx wrangler dev` run (wrangler 4.152.0 is available via npx), deploy, live smoke test.
- Secret scan of board/ (no secrets were written; client id above is public).

## Next commands (after the missing files exist and tests pass)
```
cd board
npx wrangler d1 create okc-rebellion            # put database_id into wrangler.toml
npx wrangler d1 execute okc-rebellion --remote --file schema.sql
openssl rand -hex 32 | npx wrangler secret put SESSION_SECRET
npx wrangler secret put GITHUB_CLIENT_SECRET     # owner runs this himself
npx wrangler deploy
curl -s https://rebellion.1nterface.ai/healthz
```
