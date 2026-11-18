"""Minimal Codex usage client with endpoint caching and a session-log fallback.

Reuse endpoint answers for five minutes, make at most one live request, then
accept a saved answer or a session event for up to an hour. All HTTP goes
through ``_get_json``.
"""

import http.client
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .auth import config_dir, resolve_credentials
from .report import session_timestamp, shape

URL = "https://chatgpt.com/backend-api/wham/usage"
FRESH = 300
MAX_AGE = 3600


def write_cache(path, data):
    """Replace a cache atomically, never exposing a partially written JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                      prefix=path.name + '.', suffix='.tmp', delete=False)
    try:
        with tmp:
            json.dump(data, tmp)
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards the Authorization header to redirect targets.
        return None


def _get_json(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def from_endpoint(config, verbose=False):
    token, account_id, _ = resolve_credentials(config, verbose=verbose)
    headers = {
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
        'User-Agent': f'codex-usage/{__version__}',
    }
    if account_id:
        headers['ChatGPT-Account-Id'] = account_id
    try:
        data = _get_json(URL, headers, 15)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code} from {URL}') from None
    except (OSError, http.client.HTTPException):
        raise RuntimeError(f'could not reach {URL}') from None
    except (ValueError, TypeError):
        # A rejected HTTP header can appear verbatim in the library exception.
        raise RuntimeError(f'invalid request or JSON response from {URL}') from None
    out = shape(data, 'endpoint')
    write_cache(Path(config) / 'last-usage.json', out['raw'])
    return out


def from_last(config, max_age):
    path = Path(config) / 'last-usage.json'
    try:
        age = time.time() - path.stat().st_mtime
        if age >= max_age:
            raise RuntimeError(f'last endpoint answer is {int(age // 60)} min old')
        with path.open(encoding='utf-8') as stream:
            data = json.load(stream)
    except FileNotFoundError:
        raise RuntimeError('no saved endpoint answer yet') from None
    source = f'endpoint, {int(age // 60)} min ago' if age >= 60 else 'endpoint'
    return shape(data, source)


def _newest_session(config):
    sessions = Path(config) / 'sessions'
    days = sorted(sessions.glob('[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]'), reverse=True)
    newest, latest_mtime = None, float('-inf')
    for day in days:
        for path in day.glob('rollout-*.jsonl'):
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime_ns > latest_mtime:
                newest, latest_mtime = path, info.st_mtime_ns
    return newest


def _session_snapshot(path):
    model, snapshot, stamp = None, None, None
    with path.open('rb') as stream:
        for line in stream:
            try:
                event = json.loads(line)
                payload = event.get('payload')
                if not isinstance(payload, dict):
                    continue
                if event.get('type') == 'turn_context' and isinstance(payload.get('model'), str):
                    model = payload['model']
                if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                    continue
                limits = payload.get('rate_limits')
                if (isinstance(limits, dict) and limits.get('limit_id', 'codex') == 'codex'
                        and any(isinstance(limits.get(key), dict) and limits[key]
                                for key in ('primary', 'secondary'))):
                    stamp = session_timestamp(event['timestamp'])
                    snapshot = dict(limits, timestamp=event['timestamp'])
            except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                continue
    if snapshot is None:
        raise RuntimeError('newest Codex session log has no Codex rate limits')
    if model:
        snapshot['model'] = model
    return snapshot, stamp


def from_session(config):
    path = _newest_session(config)
    if path is None:
        raise RuntimeError('no Codex session log yet')
    data, stamp = _session_snapshot(path)
    age = max(0, time.time() - stamp)
    if age >= MAX_AGE:
        raise RuntimeError(f'Codex session log usage is {int(age // 60)} min old')
    out = shape(data, f'session log, {int(age // 60)} min ago')
    if not out['windows']:
        raise RuntimeError('all Codex session log rate limits have reset')
    return out


def usage(config=None, verbose=False):
    """Return the first available source or raise its distinct failure reasons."""
    config = Path(config) if config is not None else config_dir()
    errors = []
    sources = (lambda: from_last(config, FRESH),
               lambda: from_endpoint(config, verbose=verbose),
               lambda: from_last(config, MAX_AGE),
               lambda: from_session(config))
    for source in sources:
        try:
            out = source()
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            if str(exc) not in errors:
                errors.append(str(exc))
        else:
            if verbose:
                print('usage: ' + out['source'], file=sys.stderr)
            return out
    raise RuntimeError('; '.join(errors))
