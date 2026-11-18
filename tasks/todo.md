# codex-usage: Codex plan limits from the shell

Sibling of `../claude-usage` (PyPI `claude-usage-limits`) and `../cursor-usage-cli`
(PyPI `cursor-usage`). **Mirror `../claude-usage` file for file**: layout, code style,
README structure/emojis/badges, docs, demo recorder, tests. Only the Codex-specific
protocol differs.

## Names
- PyPI distribution `codex-usage-limits` (`codex-usage` is taken on PyPI), version `0.1.0`
- Command `codex-usage`, import package `codex_usage`, repo `javaisbetterthanpython/codex-usage`
- Author/license holder: `codex-usage contributors`, MIT, zero dependencies, Python 3.9+

## Protocol (verified live 2026-11-17, codex-cli 0.169.1)
- Config dir: `CODEX_HOME` or `~/.codex` (`%USERPROFILE%\.codex` on Windows).
- `GET https://chatgpt.com/backend-api/wham/usage`
  headers: `Authorization: Bearer <tokens.access_token>`, `ChatGPT-Account-Id: <tokens.account_id>`
  (omit when unknown), `Accept: application/json`, `User-Agent: codex-usage/<version>`. 15 s timeout, no retries.
- Response (observed, Pro Lite plan):
  ```json
  {"user_id": "...", "account_id": "...", "email": "...", "plan_type": "prolite",
   "rate_limit": {"allowed": true, "limit_reached": false,
     "primary_window": {"used_percent": 0, "limit_window_seconds": 604800, "reset_after_seconds": 594548, "reset_at": 1795566796},
     "secondary_window": null},
   "code_review_rate_limit": null,
   "additional_rate_limits": null,
   "model_usage": {"gpt-6-astra": {"available": true, "available_at": null, "credits_would_enable": false}},
   "credits": {"has_credits": false, "unlimited": false, "overage_limit_reached": false, "balance": "0",
               "approx_local_messages": [0, 0], "approx_cloud_messages": [0, 0]},
   "spend_control": {"reached": false, "individual_limit": null},
   "rate_limit_reached_type": null, "promo": null,
   "rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 0}}
  ```
  `code_review_rate_limit` has the same shape as `rate_limit`. `additional_rate_limits` is a list of
  `{"limit_name", "metered_feature", "rate_limit": {...same shape...}}` (from Codex's struct names; not observed populated).
  Windows: a plan may have a 5-hour (18000 s) primary plus a 7-day (604800 s) secondary, or just a 7-day primary.
  **Never hard-code which**; label windows from their duration.
  `reset_at` is Unix epoch seconds.
- No 429 seen on 5 back-to-back calls. Keep five-minute reuse anyway (courtesy + speed).
- **Privacy:** strip top-level `email`, `user_id`, `account_id` before caching and before `--json` output.

## Credentials (read-only, never refresh, never write)
1. `CODEX_ACCESS_TOKEN` env override, used as-is; account id from `CODEX_ACCOUNT_ID` or the JWT claim.
2. `<config>/auth.json`: `tokens.access_token`, `tokens.account_id`. `auth_mode` `"chatgpt"`.
   If no tokens (API-key login: `OPENAI_API_KEY` set, `tokens` null) → error
   "an API-key login has no ChatGPT plan limits; run `codex login` to sign in with ChatGPT".
3. macOS only: Keychain service `Codex Auth` (`security find-generic-password -s "Codex Auth" -w`), same JSON,
   for `cli_auth_credentials_store = "keyring"`. Not observed locally; tried only when the file is missing.
- Expiry: decode the JWT payload (base64url, no verification) and refuse when `exp < now`:
  "the access token has expired; open `codex` so it refreshes". Undecodable JWT → don't check expiry.
- JWT claim `https://api.openai.com/auth` contains `chatgpt_account_id`, `chatgpt_plan_type`.
- `-v` prints `credentials: <source>`, `usage: <source>`, `plan: <plan_type>` (raw, from response or JWT claim;
  do NOT map to marketing names). Never print tokens, headers, emails, ids.

## Fallback source: Codex session logs (replaces Claude's status-line cache)
- Codex appends `{"timestamp": ISO, "type": "event_msg", "payload": {"type": "token_count", "info": ..., "rate_limits": {...}}}`
  lines to `<config>/sessions/YYYY/MM/DD/rollout-*.jsonl`. `rate_limits`:
  `{"limit_id": "codex", "limit_name": null, "primary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": 1795566796},
    "secondary": null, "credits": {...}, "plan_type": "prolite", "rate_limit_reached_type": null}`
  Seen locally: `primary` 300 min + `secondary` 10080 min; `primary` 10080 alone; `limit_id` `"premium"` with no windows;
  `rate_limits` null. Older Codex versions wrote `resets_in_seconds` (relative to the event timestamp) instead of `resets_at`; accept both.
- Use the newest file by mtime (walk date dirs newest-first; don't read every file), the last `token_count` event with
  `rate_limits.limit_id == "codex"` (or missing limit_id) and at least one window. Age = now − event timestamp.
  Model from the last `turn_context` payload `model` in that file (for short output).
- A window whose reset time has passed is dropped (its percentage is stale).
- Read-only. No `--statusline` flag (Codex has no command status line).

## Source order (same as claude-usage)
1. `<config>/last-usage.json` if < 300 s old (mtime)
2. one live endpoint request; save atomically (tmp + os.replace) with identity fields stripped
3. `last-usage.json` if < 3600 s old
4. newest session log event if < 3600 s old
Source strings: `endpoint`, `endpoint, N min ago` (≥ 60 s), `session log, N min ago` (always). Plain output
appends `(from the <source>)` when source != `endpoint`. All fail → distinct reasons joined `; ` →
`codex-usage: <reasons>` on stderr, exit 1.

## Normalized shape (report.py)
`shape(data, source)` → `{"source", "raw", "windows": [{"name", "label", "short", "utilization", "resets_at"}...]}`
- endpoint: `rate_limit.primary_window`, `secondary_window`, then each `additional_rate_limits[]` entry's windows
  with `name` = its `limit_name` (or `metered_feature`). `code_review_rate_limit` is detail-only (`--all`).
- session: `primary`/`secondary` with `window_minutes`.
- Labels from duration: 18000 s → `5-hour`/`5h`, 604800 → `7-day`/`7d`; generic: whole days `N-day`/`Nd`, whole hours
  `N-hour`/`Nh`, else `N-min`/`Nm`. Additional limits: `7-day <name>` / `7d <name>`. Sort main windows shortest first.
- `resets_at` ISO 8601 UTC; percentages float or None.

## CLI (mirror claude-usage cli.py)
- `codex-usage` → one line per window: `5-hour: 23% used, resets 14:00 (in 2h41m)` / `7-day: 59% used, resets Sun 03:00 (in 12h29m)`
- `--json` (alias `--format json`, indent=1, raw with identity fields stripped), `--format {plain,json,short}`
- `--warn [N]` (default 70, inclusive) any window incl. additional; `--warn-7d [N]` only the main 7-day window
  (no 7-day window → never trips)
- `--all` (plain only): table
  ```
  limit          window   percent  status     reset
  ------------------------------------------------------------------------------
  codex          5-hour       23%  allowed    resets 14:00 (in 2h41m)
  codex          7-day        59%  allowed    resets Sun 03:00 (in 12h29m)
  code review    7-day         4%  allowed    resets Sun 03:00 (in 12h29m)
  plan prolite · credits 0 · reset credits 2 available
  ```
  status: `limit hit` when `limit_reached` true or `allowed` false, else `allowed`. Session source →
  `(details not available from the session-log source)`.
- short: `7d 59%` / `5h 23% · 7d 59%`; session source prefixes the model: `gpt-6.2-sol · 5h 23% · 7d 59%`.
- `-v`, `-V` (`codex-usage 0.1.0`), exit codes 0 / 1 / 2.

## Steps
- [x] git init, scaffold: LICENSE, .gitignore, pyproject.toml, src/codex_usage/{__init__,__main__,auth,api,report,cli}.py
- [x] tests/test_auth.py, test_report.py, test_sources.py (mocked HTTP seam `_get_json`, tmp CODEX_HOME, fail on unmocked HTTP)
- [x] README.md mirroring claude-usage (badges, demo GIF raw URL, sections, emojis)
- [x] docs/HOW_THIS_WAS_BUILT.md, docs/PUBLISHING.md, docs/record_demo.py → docs/demo.gif (agg)
- [x] Review diff, pytest, live run against the real endpoint once, build + twine check
- [ ] Ask before: public GitHub repo, PyPI upload

## Review

- Built by Sol from this spec; reviewed by Claude. 96 tests pass on Python 3.12 and 3.9.
- Review fix: a window without a duration, or an additional limit with no name, is skipped instead of failing the whole endpoint answer.
- Live run against a Pro Lite account: weekly-only window, `--warn 0` exits 1, no identity fields in `--json` or the cache.
- `python -m build` + `twine check` pass. Docs URLs resolve; no tokens/ids/emails in the tree.
- Pending: user approval to create the public GitHub repo and upload to PyPI.
