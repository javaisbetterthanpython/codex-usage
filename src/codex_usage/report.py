"""Turn endpoint and session-log payloads into compact usage reports."""

import re
import time
from datetime import datetime, timezone


IDENTITY_FIELDS = ('email', 'user_id', 'account_id')


def strip_identity(data):
    if not isinstance(data, dict):
        raise ValueError("usage data is not a JSON object")
    return {key: value for key, value in data.items() if key not in IDENTITY_FIELDS}


def _label(seconds):
    for size, label, short in ((86400, 'day', 'd'), (3600, 'hour', 'h')):
        if seconds and seconds % size == 0:
            count = int(seconds // size)
            return f'{count}-{label}', f'{count}{short}'
    count = int(seconds // 60)
    return f'{count}-min', f'{count}m'


def _window(raw, seconds, reset, name, suffix=''):
    label, short = _label(seconds)
    used = raw.get('used_percent')
    return {'name': name, 'label': label + suffix, 'short': short + suffix,
            'utilization': float(used) if used is not None else None,
            'resets_at': datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset is not None else None}


def _endpoint_windows(limit, name, suffix=''):
    windows = [window for key in ('primary_window', 'secondary_window')
               if isinstance(window := limit.get(key), dict)
               and isinstance(window.get('limit_window_seconds'), (int, float))]
    windows.sort(key=lambda window: window['limit_window_seconds'])
    return [_window(window, window['limit_window_seconds'], window.get('reset_at'), name, suffix)
            for window in windows]


def _endpoint_limits(raw):
    yield 'codex', raw.get('rate_limit') or {}, ''
    for entry in raw.get('additional_rate_limits') or []:
        name = entry.get('limit_name') or entry.get('metered_feature') or 'other'
        yield name, entry.get('rate_limit') or {}, ' ' + name


def session_timestamp(iso):
    # Codex writes nanoseconds, while Python 3.9 expects 3 or 6 fractional digits.
    iso = re.sub(r'\.(\d+)', lambda match: '.' + match[1][:6].ljust(6, '0'), iso, count=1)
    timestamp = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp()


def _session_windows(raw):
    now = time.time()
    windows = [window for key in ('primary', 'secondary')
               if isinstance(window := raw.get(key), dict)
               and isinstance(window.get('window_minutes'), (int, float))]
    windows.sort(key=lambda window: window['window_minutes'])
    out = []
    for window in windows:
        reset = window.get('resets_at')
        relative = window.get('resets_in_seconds')
        if reset is None and relative is not None and raw.get('timestamp'):
            reset = session_timestamp(raw['timestamp']) + relative
        if reset is None or reset > now:
            out.append(_window(window, window['window_minutes'] * 60, reset, 'codex'))
    return out


def shape(data, source):
    """Normalize usage windows and keep the source payload with identities removed."""
    raw = strip_identity(data)
    if source.startswith('session log'):
        windows = _session_windows(raw)
    else:
        windows = [window for name, limit, suffix in _endpoint_limits(raw)
                   for window in _endpoint_windows(limit, name, suffix)]
    return {'source': source, 'raw': raw, 'windows': windows}


def warning_reached(usage, threshold, weekly_only=False):
    """Check all windows, or only the main seven-day window."""
    return any(window['utilization'] is not None and window['utilization'] >= threshold
               for window in usage['windows'] if not weekly_only or window['label'] == '7-day')


def when(iso):
    if not iso:
        return "reset time unknown"
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    now = datetime.now(t.tzinfo)
    mins = max(0, int((t - now).total_seconds() // 60))
    h, m = divmod(mins, 60)
    span = f"{h}h{m:02d}m" if h else f"{m}m"
    day = "" if t.date() == now.date() else t.strftime("%a ")
    return f"resets {day}{t:%H:%M} (in {span})"


def pct(window):
    used = window.get("utilization")
    return "?" if used is None else f"{used:.0f}%"


def render_plain(usage):
    out = []
    for window in usage['windows']:
        out.append(f"{window['label']}: {pct(window)} used, {when(window['resets_at'])}")
    if usage['source'] != 'endpoint':
        out.append(f"(from the {usage['source']})")
    return '\n'.join(out)


def render_short(usage):
    """Render a compact line, with a model name for session-log answers."""
    model = usage['raw'].get('model') if usage['source'].startswith('session log') else None
    parts = [model] if model else []
    for window in usage['windows']:
        if window['utilization'] is not None:
            parts.append(f"{window['short']} {pct(window)}")
    return ' · '.join(parts)


def render_limits(usage):
    """Render per-limit detail and the account summary, when available."""
    if usage['source'].startswith('session log'):
        return '(details not available from the session-log source)'
    raw = usage['raw']
    row = '%-14s %-8s %7s  %-10s %s'
    out = []
    limits = list(_endpoint_limits(raw))
    limits.append(('code review', raw.get('code_review_rate_limit') or {}, ''))
    for name, limit, _ in limits:
        status = 'limit hit' if limit.get('limit_reached') or limit.get('allowed') is False else 'allowed'
        for window in _endpoint_windows(limit, name):
            out.append(row % (name, window['label'], pct(window), status, when(window['resets_at'])))
    if out:
        out[:0] = [row % ('limit', 'window', 'percent', 'status', 'reset'), '-' * 78]
    summary = []
    if raw.get('plan_type'):
        summary.append(f"plan {raw['plan_type']}")
    balance = (raw.get('credits') or {}).get('balance')
    if balance is not None:
        summary.append(f'credits {balance}')
    resets = (raw.get('rate_limit_reset_credits') or {}).get('available_count')
    if resets is not None:
        summary.append(f'reset credits {resets} available')
    if summary:
        out.append(' · '.join(summary))
    return '\n'.join(out)
