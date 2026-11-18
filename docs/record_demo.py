#!/usr/bin/env python3
"""Render docs/demo.gif with agg using the real CLI and illustrative local data.

Run from a checkout: python3 docs/record_demo.py
Requires agg (https://github.com/asciinema/agg) on PATH. No credentials or network
are used by the recorded commands. The temporary asciicast is deleted afterward.
Set CODEX_USAGE_DEMO_FONT_DIR if DejaVu Sans Mono is outside agg's font search path.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_CLI = '''
from codex_usage import api, auth
from codex_usage.cli import main

def no_http(*args, **kwargs):
    raise AssertionError("the demo attempted an HTTP request")

api._get_json = no_http
auth._from_keychain = lambda: None
raise SystemExit(main())
'''


def main():
    agg = shutil.which('agg')
    if not agg:
        raise SystemExit('Install agg from https://github.com/asciinema/agg/releases first.')

    now = datetime.now(timezone.utc)
    short_reset = (now + timedelta(hours=2, minutes=42)).replace(second=0, microsecond=0)
    weekly_reset = (now + timedelta(days=2)).replace(hour=3, minute=0, second=0, microsecond=0)
    short = {'used_percent': 23, 'limit_window_seconds': 18000, 'reset_at': int(short_reset.timestamp())}
    weekly = {'used_percent': 59, 'limit_window_seconds': 604800, 'reset_at': int(weekly_reset.timestamp())}
    payload = {
        'plan_type': 'pro',
        'rate_limit': {'allowed': True, 'limit_reached': False,
                       'primary_window': short, 'secondary_window': weekly},
        'code_review_rate_limit': {'allowed': True, 'limit_reached': False,
                                   'primary_window': dict(weekly, used_percent=4), 'secondary_window': None},
        'credits': {'balance': '0'},
        'rate_limit_reset_credits': {'available_count': 2},
    }
    stamp = now - timedelta(minutes=3)
    session = {'timestamp': stamp.isoformat(), 'type': 'event_msg', 'payload': {
        'type': 'token_count', 'rate_limits': {
            'limit_id': 'codex', 'plan_type': 'pro',
            'primary': {'used_percent': 23, 'window_minutes': 300, 'resets_at': short['reset_at']},
            'secondary': {'used_percent': 59, 'window_minutes': 10080, 'resets_at': weekly['reset_at']},
        },
    }}
    events = []
    elapsed = 0.0

    def emit(text, delay=0.0):
        nonlocal elapsed
        elapsed += delay
        events.append([round(elapsed, 3), 'o', text.replace('\n', '\r\n')])

    def page():
        emit('\x1b[2J\x1b[H\x1b[?25l'
             '\x1b[1;36mcodex-usage | Codex plan limits\x1b[0m\n'
             '\x1b[90mIllustrative data | local cache | no network\x1b[0m\n\n')

    with tempfile.TemporaryDirectory(prefix='codex-usage-demo-') as directory:
        config = Path(directory)
        (config / 'last-usage.json').write_text(json.dumps(payload), encoding='utf-8')
        offline = config / 'offline'
        session_dir = offline / 'sessions' / stamp.strftime('%Y/%m/%d')
        session_dir.mkdir(parents=True)
        (session_dir / 'rollout-demo.jsonl').write_text(json.dumps(session) + '\n', encoding='utf-8')
        env = dict(os.environ, CODEX_HOME=directory, TZ='UTC',
                   PYTHONPATH=str(ROOT / 'src'), PYTHONIOENCODING='utf-8')
        env.pop('CODEX_ACCESS_TOKEN', None)
        env.pop('CODEX_ACCOUNT_ID', None)

        def record(args, expected=0, show_exit=False, session_only=False):
            command = 'codex-usage' + ((' ' + ' '.join(args)) if args else '')
            if show_exit:
                command += '; echo "exit: $?"'
            emit('\x1b[32m$ \x1b[0m')
            for char in command:
                emit(char, 0.035)
            emit('\n', 0.2)
            command_env = dict(env, CODEX_HOME=str(offline)) if session_only else env
            result = subprocess.run([sys.executable, '-c', LOCAL_CLI] + args,
                                    capture_output=True, text=True,
                                    cwd=ROOT, env=command_env, timeout=15)
            if result.returncode != expected or result.stderr:
                raise RuntimeError(f'{command} failed: {result.stderr}')
            if session_only and '(from the session log, ' not in result.stdout:
                raise RuntimeError('The offline scene did not use the session log.')
            emit(result.stdout, 0.15)
            if show_exit:
                emit(f'exit: {result.returncode}\n')

        page()
        record([])
        elapsed += 4
        page()
        record(['--all'])
        elapsed += 6
        page()
        record(['--warn', '50'], expected=1, show_exit=True)
        elapsed += 4
        page()
        record(['--format', 'short'])
        emit('\n\x1b[90mOffline? The newest Codex session log answers:\x1b[0m\n\n', 2)
        record([], session_only=True)
        cast = config / 'demo.cast'
        with cast.open('w', encoding='utf-8') as stream:
            stream.write(json.dumps({'version': 2, 'width': 96, 'height': 20,
                                     'title': 'codex-usage plan limits demo'}) + '\n')
            for event in events:
                stream.write(json.dumps(event) + '\n')
        font_dir = os.environ.get('CODEX_USAGE_DEMO_FONT_DIR')
        font_args = ['--font-dir', font_dir] if font_dir else []
        subprocess.run([agg, *font_args, '--theme', 'github-dark', '--font-size', '17',
                        '--font-family', 'DejaVu Sans Mono', '--idle-time-limit', '8',
                        '--last-frame-duration', '5', str(cast), str(ROOT / 'docs/demo.gif')],
                       check=True)
    print('Regenerated docs/demo.gif')


if __name__ == '__main__':
    main()
