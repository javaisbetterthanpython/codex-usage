# How this tool was built (a guide for the next LLM/engineer)

This is a reproducible recipe for rebuilding `codex-usage`, following the
layout of the sibling `claude-usage` and `cursor-usage` projects. Version 0.1.0
uses the protocol verified on 2026-11-17 with Codex CLI 0.169.1. It shows the
usage windows the source reports and keeps the raw usage payload with identity
fields removed.

## 1. The goal and the missing command

Codex's interactive `/status` screen shows your plan limits. This CLI makes
those numbers available from a shell, so scripts, hooks, and agents can decide
when to back off. [Codex CLI slash commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)

## 2. The endpoint Codex calls

Running `strings` on the Codex 0.169.1 binary revealed `/wham/usage` and the
`ChatGPT-Account-Id` header. The verified request is:

```http
GET https://chatgpt.com/backend-api/wham/usage
Authorization: Bearer <tokens.access_token>
ChatGPT-Account-Id: <tokens.account_id>
Accept: application/json
User-Agent: codex-usage/0.1.0
```

The endpoint is undocumented and uses the existing ChatGPT access token.
Include the account header when the ID is known. The stdlib `urllib` request
uses a 15-second timeout. Redirects are disabled so the token stays on
`chatgpt.com`.

## 3. Finding the token locally

Resolve credentials in this order.

1. `CODEX_ACCESS_TOKEN`, used as-is. Prefer `CODEX_ACCOUNT_ID` over the ID in
   the token's JWT claim.
2. `<config>/auth.json`. Read `tokens.access_token` and `tokens.account_id`.
   An API-key login with a populated `OPENAI_API_KEY` field and no tokens has
   no ChatGPT plan limits. Tell the user to run `codex login`.
3. If the file is missing on macOS, try Keychain service **`Codex Auth`** with
   `security find-generic-password -s "Codex Auth" -w`. This uses the same JSON
   shape for `cli_auth_credentials_store = "keyring"`. This store was
   covered with mocks during local verification.

`config_dir = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")`.
The default file is `~/.codex/auth.json` on macOS/Linux and
`%USERPROFILE%\.codex\auth.json` on Windows. [Codex authentication](https://learn.chatgpt.com/docs/auth)

```json
{
  "auth_mode": "chatgpt",
  "OPENAI_API_KEY": null,
  "tokens": {
    "access_token": "<redacted>",
    "refresh_token": "<redacted>",
    "account_id": "<redacted>"
  }
}
```

Decode the JWT payload from base64url without verifying its signature. Its
`exp` is in epoch seconds. Reject a token when `exp < time.time()` and tell the
user to open `codex` so it refreshes. Skip the local check if the expiry cannot
be decoded. The `https://api.openai.com/auth` claim contains
`chatgpt_account_id` and `chatgpt_plan_type`. Verbose output reports the
credential source when used, the usage source, and the raw plan from the
response or the JWT claim. Codex handles refresh and credential writes.

## 4. The observed response and fields that matter

The Pro Lite account checked on 2026-11-17 returned one weekly window as
`rate_limit.primary_window` and a null `secondary_window`. Identity values
are redacted below.

```json
{
  "user_id": "<redacted>",
  "account_id": "<redacted>",
  "email": "<redacted>",
  "plan_type": "prolite",
  "rate_limit": {
    "allowed": true,
    "limit_reached": false,
    "primary_window": {
      "used_percent": 0,
      "limit_window_seconds": 604800,
      "reset_after_seconds": 594548,
      "reset_at": 1795566796
    },
    "secondary_window": null
  },
  "code_review_rate_limit": null,
  "additional_rate_limits": null,
  "model_usage": {
    "gpt-6-astra": {"available": true, "available_at": null, "credits_would_enable": false}
  },
  "credits": {
    "has_credits": false,
    "unlimited": false,
    "overage_limit_reached": false,
    "balance": "0",
    "approx_local_messages": [0, 0],
    "approx_cloud_messages": [0, 0]
  },
  "spend_control": {"reached": false, "individual_limit": null},
  "rate_limit_reached_type": null,
  "promo": null,
  "rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 0}
}
```

- Read `used_percent` as a float and `reset_at` as epoch seconds. Normalize
  resets to ISO 8601 UTC. Render local reset times and clamp elapsed countdowns
  to zero.
- Label windows from `limit_window_seconds`. A weekly window can be primary or
  secondary. Sort the main windows by duration and label whole days, then
  whole hours, then minutes.
- Codex's structs describe `additional_rate_limits` as a list of objects with
  `limit_name`, `metered_feature`, and a nested `rate_limit`. The list was null
  in the observed response. Append the name to each additional window label,
  using `metered_feature` when the name is absent.
- `code_review_rate_limit` has the same shape as `rate_limit`. Show it under
  `--all`. A rate limit shows `limit hit` when `limit_reached` is true or
  `allowed` is false. The table footer includes the plan, credit balance,
  and available reset credits when present.
- Before saving or printing JSON, remove top-level `email`, `user_id`, and
  `account_id`. Keep the remaining fields, including nulls.

## 5. The cache and observed endpoint behavior

Five back-to-back calls returned no HTTP 429. Keep five-minute reuse to reduce
requests and make repeated CLI runs faster.

1. Read `last-usage.json` if younger than **300 seconds** (`FRESH`).
2. Try the live endpoint once and atomically save a successful answer.
3. If that fails, accept `last-usage.json` younger than **3600 seconds** (`MAX_AGE`).
4. If that fails, accept the newest session log's Codex usage event if younger
   than **3600 seconds** and it has a usable window.

Use the cache file's mtime and the log event's timestamp for ages. Write the
cache to a temporary file in the same directory and replace it with
`os.replace` so concurrent readers see a complete JSON document. If all sources
fail, collect distinct reasons in source order, join with `; `, print
`codex-usage: <reasons>` to stderr, and exit 1.

## 6. The fallback from Codex session logs

Codex appends JSON lines to `<config>/sessions/YYYY/MM/DD/rollout-*.jsonl`. A
token count event can include a snapshot like this:

```json
{
  "timestamp": "2026-11-17T03:08:00Z",
  "type": "event_msg",
  "payload": {
    "type": "token_count",
    "info": null,
    "rate_limits": {
      "limit_id": "codex",
      "limit_name": null,
      "primary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": 1795566796},
      "secondary": null,
      "credits": null,
      "plan_type": "prolite",
      "rate_limit_reached_type": null
    }
  }
}
```

Walk date directories newest first and use file mtimes to select the newest
rollout. Read that file and keep the last `token_count` event with Codex limits
and at least one window. Accept a missing `limit_id` for older logs. Skip
`premium` and null snapshots. For a legacy `resets_in_seconds` window, add the
offset to the event timestamp. Drop windows from the report after their reset
time has passed.

Use the last `turn_context` model for short output, for example
`gpt-6.2-sol · 5h 23% · 7d 59%`. Session-log JSON contains the selected
`rate_limits` fields, the event timestamp, and the model when available.
The CLI reads logs as-is, and they update only while Codex runs.
[Codex rollout source](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/common/rollout.rs)

## 7. Turning the script into a package

Follow the sibling project's setuptools and `src/` layout, module docstrings,
small functions, pytest assertions, centered README header, and MIT license.

- `auth.py` resolves credentials, checks token expiry, and exposes the plan
  claim for verbose mode.
- `api.py` uses an injectable config directory, one `_get_json` HTTP seam,
  atomic cache writes, and distinct source failures. Tests use temporary
  config directories, a mocked HTTP seam, and `os.utime` for file ages.
- `report.py` normalizes windows while retaining sanitized raw data and the
  usage source. It renders local reset times and percentages.
- `cli.py` uses argparse and `main(argv=None)` returns an int.
  `__main__.py` uses `sys.exit(main())`.

`--json` and `--format` share the same argparse destination, so the last wins.
JSON uses `indent=1`, and `--all` adds detail in plain format. `--warn [N]`
checks all normalized windows, while `--warn-7d [N]` checks only the main
7-day window. Both default to 70% when supplied without a number. Missing
utilization never trips a threshold. Exit codes are 0 for success, 1 for a
threshold or unavailable data, and 2 for argparse errors.

The PyPI distribution is **`codex-usage-limits`** because `codex-usage` is taken.
The repo and console command are `codex-usage`, and the import is `codex_usage`.

## 8. Cross-OS notes

Only the Keychain fallback is macOS-specific. All three platforms can use
`auth.json`, with `pathlib` for paths, `datetime` for timestamps, and `urllib`
for HTTP. The CLI has no runtime dependencies.

Python 3.9 is the compatibility floor. Normalize fractional seconds in
Codex's event timestamps before parsing to handle nanosecond precision on
Python 3.9. Windows file and HTTP behavior use the standard library paths.

## 9. How to re-verify quickly (one-liner, macOS and Linux)

Run this once with a ChatGPT login in `~/.codex/auth.json`. Python keeps the
token in-process and prints the response with identity fields removed. The
command bypasses the CLI cache.

```bash
python3 -c 'import http.client, json, sys; from pathlib import Path; sys.excepthook = lambda *_: print("Codex usage check failed", file=sys.stderr); t = json.loads((Path.home() / ".codex/auth.json").read_text(encoding="utf-8"))["tokens"]; h = {"Authorization": "Bearer " + t["access_token"], "Accept": "application/json", "User-Agent": "codex-usage/0.1.0"}; h.update({"ChatGPT-Account-Id": t["account_id"]} if t.get("account_id") else {}); c = http.client.HTTPSConnection("chatgpt.com", timeout=15); c.request("GET", "/backend-api/wham/usage", headers=h); r = c.getresponse(); print(json.dumps({k: v for k, v in json.load(r).items() if k not in ("email", "user_id", "account_id")}, indent=1)) if r.status == 200 else sys.exit(f"HTTP {r.status}"); c.close()'
```

A response with a `rate_limit` object confirms the protocol. For ordinary
verification, use the mocked suite.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest -q
.venv/bin/codex-usage --help
.venv/bin/codex-usage --version
```

## Reference links

- [Codex CLI slash commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex rollout source](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/common/rollout.rs)
- [Sibling project with the same layout and publishing flow](https://github.com/javaisbetterthanpython/claude-usage)
