import base64
import io
import json
import os
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from codex_usage import api, auth, cli

WEEKLY = {"used_percent": 59, "limit_window_seconds": 604800, "reset_at": 1795566796}
ENDPOINT = {"rate_limit": {"primary_window": WEEKLY, "secondary_window": None}, "plan_type": "prolite"}
IDENTITY = {"email": "private@example.invalid", "user_id": "PRIVATE-USER", "account_id": "PRIVATE-ACCOUNT"}
NOW = 1794900000


@pytest.fixture(autouse=True)
def local_only(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "PRIVATE-TOKEN")
    monkeypatch.delenv("CODEX_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(auth.sys, "platform", "linux")
    get_json = api._get_json
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: pytest.fail("unmocked HTTP call"))
    return get_json


def save(tmp_path, name, data, age=0):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def rate_limited(*args, **kwargs):
    raise urllib.error.HTTPError(api.URL, 429, "PRIVATE-TOKEN", {}, None)


def session_window(percent=59, minutes=10080, **reset):
    return dict(used_percent=percent, window_minutes=minutes, **reset)


def rate_event(limits, age=120):
    stamp = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()
    return {"timestamp": stamp, "type": "event_msg", "payload": {
        "type": "token_count", "info": None, "rate_limits": limits,
    }}


def save_session(tmp_path, events, day='17', name='example', age=0):
    path = tmp_path / 'sessions' / '2026' / '11' / day / f'rollout-{name}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join((event if isinstance(event, str) else json.dumps(event)) + '\n'
                            for event in events))
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def test_fresh_skips_endpoint_and_credentials(monkeypatch, tmp_path, capsys):
    save(tmp_path, 'last-usage.json', ENDPOINT, 180)
    monkeypatch.setattr(api, "resolve_credentials", lambda *a, **k: pytest.fail("credentials called"))
    out = api.usage(verbose=True)
    assert out['raw'] == ENDPOINT
    assert out['source'] == 'endpoint, 3 min ago'
    assert capsys.readouterr().err == 'usage: endpoint, 3 min ago\n'


@pytest.mark.parametrize('age,source', [(59, 'endpoint'), (60, 'endpoint, 1 min ago'),
                                         (299, 'endpoint, 4 min ago')])
def test_fresh_source_ages(monkeypatch, tmp_path, age, source):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    save(tmp_path, 'last-usage.json', ENDPOINT, age)
    assert api.usage()['source'] == source


@pytest.mark.parametrize('account_id', [None, 'PRIVATE-ACCOUNT'])
def test_endpoint_success_saves_atomically(monkeypatch, tmp_path, capsys, account_id):
    if account_id:
        monkeypatch.setenv('CODEX_ACCOUNT_ID', account_id)

    def get(url, headers, timeout):
        assert url == 'https://chatgpt.com/backend-api/wham/usage'
        expected = {"Authorization": "Bearer PRIVATE-TOKEN", "Accept": "application/json",
                    "User-Agent": "codex-usage/0.1.0"}
        if account_id:
            expected["ChatGPT-Account-Id"] = account_id
        assert headers == expected
        assert timeout == 15
        return dict(ENDPOINT, **IDENTITY)

    monkeypatch.setattr(api, '_get_json', get)
    assert cli.main(['--json', '-v']) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == ENDPOINT
    assert json.loads((tmp_path / 'last-usage.json').read_text()) == ENDPOINT
    assert not list(tmp_path.glob('*.tmp'))
    assert err == 'credentials: CODEX_ACCESS_TOKEN\nusage: endpoint\nplan: prolite\n'


def test_failed_replace_leaves_prior_cache(monkeypatch, tmp_path):
    path = save(tmp_path, 'last-usage.json', ENDPOINT)

    def fail(source, target):
        assert json.loads(Path(source).read_text()) == {"replacement": True}
        assert target == path
        raise OSError('replace blocked')

    monkeypatch.setattr(api.os, "replace", fail)
    with pytest.raises(OSError, match='replace blocked'):
        api.write_cache(path, {"replacement": True})
    assert json.loads(path.read_text()) == ENDPOINT
    assert list(tmp_path.iterdir()) == [path]


def test_endpoint_is_tried_once_when_fresh_cache_expires(monkeypatch, tmp_path):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    save(tmp_path, 'last-usage.json', ENDPOINT, 300)
    calls = []

    def get(*args):
        calls.append(args)
        return dict(ENDPOINT, plan_type='pro')

    monkeypatch.setattr(api, '_get_json', get)
    assert api.usage()['raw']['plan_type'] == 'pro'
    assert len(calls) == 1


@pytest.mark.parametrize('age', [300, 600, 3599])
def test_429_uses_saved_endpoint_before_session(monkeypatch, tmp_path, age):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    save(tmp_path, 'last-usage.json', ENDPOINT, age)
    save_session(tmp_path, [rate_event({"primary": session_window(resets_at=NOW + 3600)})])
    monkeypatch.setattr(api, '_get_json', rate_limited)
    assert api.usage()['source'] == f'endpoint, {age // 60} min ago'


def test_session_uses_newest_file_by_mtime_and_only_reads_that_file(monkeypatch, tmp_path):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    limits = {"limit_id": "codex", "primary": session_window(resets_at=NOW + 3600)}
    newer_directory = save_session(tmp_path, [rate_event(limits, 60)], day='18', age=120)
    newest = save_session(tmp_path, [rate_event(limits, 12 * 60)], name='a', age=60)
    older_file = save_session(tmp_path, [rate_event(limits, 60)], name='z', age=90)
    open_file = Path.open

    def checked_open(path, *args, **kwargs):
        assert path not in (newer_directory, older_file)
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', checked_open)
    assert api._newest_session(tmp_path) == newest
    out = api.from_session(tmp_path)
    assert out['source'] == 'session log, 12 min ago'
    assert out['windows'][0]['utilization'] == 59


def test_session_uses_last_codex_event_and_last_model(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    limits = {"limit_id": "codex", "plan_type": "pro", "primary": session_window(5, 300, resets_at=NOW + 3600)}
    latest = {"primary": session_window(23, 300, resets_at=NOW + 3600),
              "secondary": session_window(resets_at=NOW + 2 * 86400), "plan_type": "pro"}
    last = rate_event(latest, 2 * 60)
    last['timestamp'] = last['timestamp'].replace('+00:00', '.123456789Z')
    save_session(tmp_path, [
        {"type": "turn_context", "payload": {"model": "previous-model"}},
        rate_event(limits, 180), last,
        rate_event({"limit_id": "premium", "primary": session_window(80, resets_at=NOW + 3600)}, 60),
        rate_event({"limit_id": "codex", "primary": None, "secondary": None}, 45),
        rate_event(None, 30), "{truncated",
        {"type": "turn_context", "payload": {"model": "gpt-6.2-sol"}},
    ])
    save(tmp_path, 'last-usage.json', ENDPOINT, 3600)
    monkeypatch.setattr(api, '_get_json', rate_limited)
    assert cli.main(['--all']) == 0
    out = capsys.readouterr().out
    assert out.startswith('5-hour: 23% used,')
    assert '7-day: 59% used,' in out
    assert '(from the session log, 1 min ago)' in out
    assert '(details not available from the session-log source)' in out
    assert cli.main(['--format', 'short']) == 0
    assert capsys.readouterr().out == 'gpt-6.2-sol · 5h 23% · 7d 59%\n'
    assert cli.main(['--json']) == 0
    assert json.loads(capsys.readouterr().out) == dict(latest, timestamp=last['timestamp'], model='gpt-6.2-sol')


def test_legacy_relative_reset_uses_event_time_and_past_resets_are_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    expired = session_window(70, 300, resets_at=NOW, resets_in_seconds=3600)
    legacy = session_window(resets_in_seconds=600)
    save_session(tmp_path, [rate_event({"primary": expired, "secondary": legacy}, 180)])
    out = api.from_session(tmp_path)
    assert out['source'] == 'session log, 3 min ago'
    assert [window['short'] for window in out['windows']] == ['7d']
    assert out['windows'][0]['resets_at'] == datetime.fromtimestamp(NOW + 420, timezone.utc).isoformat()


def test_newest_event_with_no_live_windows_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    save_session(tmp_path, [
        rate_event({"primary": session_window(resets_at=NOW + 3600)}, 600),
        rate_event({"primary": session_window(resets_in_seconds=60)}, 120),
    ])
    with pytest.raises(RuntimeError, match='all Codex session log rate limits have reset'):
        api.from_session(tmp_path)


def test_session_without_auth_never_calls_http(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    monkeypatch.delenv('CODEX_ACCESS_TOKEN')
    save_session(tmp_path, [rate_event({"primary": session_window(resets_at=NOW + 3600)}, 0)])
    assert cli.main([]) == 0
    assert capsys.readouterr().out.endswith('(from the session log, 0 min ago)\n')
    assert not (tmp_path / 'last-usage.json').exists()


def test_all_fail_distinct_reasons_once(monkeypatch, capsys):
    monkeypatch.setattr(api, '_get_json', rate_limited)
    assert cli.main([]) == 1
    assert capsys.readouterr() == ('', 'codex-usage: no saved endpoint answer yet; HTTP 429 from '
                                 + api.URL + '; no Codex session log yet\n')


def test_old_session_event_rejected_even_if_file_is_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(api.time, 'time', lambda: NOW)
    save_session(tmp_path, [rate_event({"primary": session_window(resets_at=NOW + 3600)}, 3600)])
    with pytest.raises(RuntimeError, match='Codex session log usage is 60 min old'):
        api.from_session(tmp_path)


def test_no_matching_rate_limits(monkeypatch, tmp_path):
    save_session(tmp_path, [rate_event({"limit_id": "premium", "primary": None}),
                            rate_event({"other": "field"}), rate_event(None)])
    monkeypatch.setattr(api, '_get_json', rate_limited)
    with pytest.raises(RuntimeError, match='newest Codex session log has no Codex rate limits'):
        api.usage()


@pytest.mark.parametrize('error, message', [
    (urllib.error.URLError('Bearer PRIVATE-TOKEN'), 'could not reach'),
    (TimeoutError('Bearer PRIVATE-TOKEN'), 'could not reach'),
    (ValueError("Invalid header value b'Bearer PRIVATE-TOKEN'"), 'invalid request or JSON response'),
])
def test_request_errors_never_print_credentials(monkeypatch, capsys, error, message):
    def get(*args):
        raise error

    monkeypatch.setattr(api, '_get_json', get)
    assert cli.main(['-v']) == 1
    captured = capsys.readouterr()
    assert 'PRIVATE-TOKEN' not in captured.out + captured.err
    assert 'Bearer' not in captured.err
    assert message in captured.err


def test_invalid_endpoint_cache_falls_through(monkeypatch, tmp_path):
    (tmp_path / 'last-usage.json').write_text('broken JSON')
    monkeypatch.setattr(api, '_get_json', lambda *args: ENDPOINT)
    assert api.usage()['raw'] == ENDPOINT


def test_cache_json_output_strips_legacy_identity(tmp_path, capsys):
    save(tmp_path, 'last-usage.json', dict(ENDPOINT, **IDENTITY))
    assert cli.main(['--json']) == 0
    assert json.loads(capsys.readouterr().out) == ENDPOINT


def test_new_config_directory_is_created(monkeypatch, tmp_path):
    config = tmp_path / 'new-config'
    monkeypatch.setattr(api, '_get_json', lambda *args: ENDPOINT)
    assert api.usage(config=config)['raw'] == ENDPOINT
    assert (config / 'last-usage.json').exists()


def test_get_json_uses_urllib_without_redirects(monkeypatch, local_only):
    # Exercise the real HTTP seam with an in-memory response, never a socket.
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == api.URL
            assert request.get_method() == 'GET'
            assert request.get_header('Authorization') == 'Bearer test'
            assert timeout == 15
            return io.StringIO(json.dumps(ENDPOINT))

    def build_opener(handler):
        assert isinstance(handler, urllib.request.HTTPRedirectHandler)
        assert handler.redirect_request(None, None, 302, None, None, 'https://example.invalid') is None
        return Opener()

    monkeypatch.setattr(api.urllib.request, 'build_opener', build_opener)
    assert local_only(api.URL, {'Authorization': 'Bearer test'}, 15) == ENDPOINT


@pytest.mark.parametrize("endpoint_plan,expected", [('prolite', 'prolite'), (None, 'pro')])
def test_verbose_uses_response_plan_then_jwt(monkeypatch, capsys, endpoint_plan, expected):
    claims = {auth.AUTH_CLAIM: {"chatgpt_plan_type": "pro", "chatgpt_account_id": "PRIVATE-ACCOUNT"}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    token = 'header.' + payload + '.signature'
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", token)
    raw = dict(ENDPOINT, plan_type=endpoint_plan, **IDENTITY)
    monkeypatch.setattr(api, '_get_json', lambda *args: raw)
    assert cli.main(['-v']) == 0
    out, err = capsys.readouterr()
    assert out.startswith('7-day: 59% used,')
    assert err == f'credentials: CODEX_ACCESS_TOKEN\nusage: endpoint\nplan: {expected}\n'
    for secret in (*IDENTITY.values(), token):
        assert secret not in out + err


def test_unreadable_plan_is_ignored(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv('CODEX_ACCESS_TOKEN')
    (tmp_path / 'auth.json').write_text('malformed PRIVATE-TOKEN')
    save(tmp_path, 'last-usage.json', dict(ENDPOINT, plan_type=None))
    assert cli.main(['-v']) == 0
    assert capsys.readouterr().err == 'usage: endpoint\n'
