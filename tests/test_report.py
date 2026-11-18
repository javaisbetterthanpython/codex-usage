import json
from datetime import datetime, timedelta, timezone

import pytest

from codex_usage import api, auth, cli, report

# Representative weekly-only payload with illustrative values.
WEEKLY = {"used_percent": 59, "limit_window_seconds": 604800, "reset_at": 1795566796}
FIVE_HOUR = {"used_percent": 23, "limit_window_seconds": 18000, "reset_at": 1794966796}
ENDPOINT = {
    "plan_type": "prolite",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": WEEKLY, "secondary_window": None},
    "code_review_rate_limit": None,
    "additional_rate_limits": None,
    "credits": {"balance": "0"},
    "rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 0},
}
WITH_SESSION = dict(ENDPOINT, rate_limit=dict(ENDPOINT['rate_limit'], secondary_window=FIVE_HOUR))
SESSION = {"model": "gpt-6.2-sol",
           "primary": {"used_percent": 23, "window_minutes": 300, "resets_at": 1794966796},
           "secondary": {"used_percent": 59, "window_minutes": 10080, "resets_at": 1795566796}}


@pytest.fixture(autouse=True)
def local_only(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: pytest.fail("unmocked HTTP call"))
    monkeypatch.setattr(cli, "usage", lambda **kwargs: report.shape(ENDPOINT, "endpoint"))


def test_shape_endpoint():
    out = report.shape(ENDPOINT, "endpoint")
    assert out == {
        "source": "endpoint", "raw": ENDPOINT, "windows": [
            {"name": "codex", "label": "7-day", "short": "7d", "utilization": 59.0,
             "resets_at": datetime.fromtimestamp(WEEKLY["reset_at"], timezone.utc).isoformat()},
        ],
    }
    with_session = report.shape(WITH_SESSION, "endpoint")["windows"]
    assert with_session == [
        {"name": "codex", "label": "5-hour", "short": "5h", "utilization": 23.0,
         "resets_at": datetime.fromtimestamp(FIVE_HOUR["reset_at"], timezone.utc).isoformat()},
        out["windows"][0],
    ]
    assert report.shape({}, "endpoint")["windows"] == []


def test_shape_session_log(monkeypatch):
    monkeypatch.setattr(report.time, "time", lambda: 1794900000)
    out = report.shape(SESSION, "session log, 12 min ago")
    assert out["windows"] == report.shape(WITH_SESSION, "endpoint")["windows"]
    assert out["raw"] == SESSION


@pytest.mark.parametrize("seconds,label,short", [
    (172800, "2-day", "2d"), (10800, "3-hour", "3h"), (5400, "90-min", "90m"),
])
def test_duration_labels(seconds, label, short):
    raw = {"rate_limit": {"primary_window": {"limit_window_seconds": seconds}}}
    window, = report.shape(raw, "endpoint")["windows"]
    assert (window["label"], window["short"]) == (label, short)
    assert window["utilization"] is window["resets_at"] is None


def test_additional_rate_limits(monkeypatch, capsys):
    raw = dict(ENDPOINT, additional_rate_limits=[
        {"limit_name": "Spark", "metered_feature": "feature-a",
         "rate_limit": {"primary_window": dict(WEEKLY, used_percent=70)}},
        {"limit_name": None, "metered_feature": "feature-b",
         "rate_limit": {"primary_window": dict(FIVE_HOUR, used_percent=10)}},
    ])
    shaped = report.shape(raw, 'endpoint')
    monkeypatch.setattr(cli, "usage", lambda **kwargs: shaped)
    assert [window["name"] for window in shaped["windows"]] == ["codex", "Spark", "feature-b"]
    assert report.render_short(shaped) == "7d 59% · 7d Spark 70% · 5h feature-b 10%"
    assert "7-day Spark: 70% used" in report.render_plain(shaped)
    assert cli.main(["--warn"]) == 1
    assert cli.main(["--warn-7d"]) == 0
    assert cli.main(["--warn", "71"]) == 0


def test_when(monkeypatch):
    local_tz = datetime(2026, 10, 24).astimezone().tzinfo
    now = datetime(2026, 10, 24, 14, 31, tzinfo=local_tz)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(report, "datetime", Clock)
    assert report.when((now + timedelta(minutes=99)).isoformat()) == "resets 16:10 (in 1h39m)"
    assert report.when((now + timedelta(minutes=7)).isoformat()) == "resets 14:38 (in 7m)"
    assert report.when((now + timedelta(days=1)).isoformat()) == "resets Sun 14:31 (in 24h00m)"
    assert report.when((now - timedelta(minutes=2)).isoformat()) == "resets 14:29 (in 0m)"
    assert report.when(None) == "reset time unknown"


@pytest.mark.parametrize("value,expected", [(23, "23%"), (23.6, "24%"), (0, "0%"), (None, "?")])
def test_pct(value, expected):
    assert report.pct({"utilization": value}) == expected
    assert report.pct({}) == "?"


@pytest.mark.parametrize("raw,first_line", [
    (ENDPOINT, ""),
    (WITH_SESSION, "5-hour: 23% used, reset time unknown\n"),
])
def test_plain_exact_layout(monkeypatch, raw, first_line):
    monkeypatch.setattr(report, "when", lambda iso: "reset time unknown")
    out = report.render_plain(report.shape(raw, "endpoint, 3 min ago"))
    assert out == (first_line + "7-day: 59% used, reset time unknown\n"
                   "(from the endpoint, 3 min ago)")


def test_cli_all(monkeypatch, capsys):
    raw = dict(WITH_SESSION, code_review_rate_limit={
        "allowed": False, "primary_window": dict(WEEKLY, used_percent=4),
    }, additional_rate_limits=[
        {"limit_name": "Spark", "rate_limit": {
            "allowed": True, "limit_reached": True, "primary_window": dict(WEEKLY, used_percent=99),
        }},
    ])
    monkeypatch.setattr(report, "when", lambda iso: "resets 16:10 (in 1h39m)")
    monkeypatch.setattr(cli, "usage", lambda **kwargs: report.shape(raw, "endpoint"))
    assert cli.main(["--all", "--warn", "100"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "5-hour: 23% used, resets 16:10 (in 1h39m)"
    assert out[1] == "7-day: 59% used, resets 16:10 (in 1h39m)"
    assert out[2] == "7-day Spark: 99% used, resets 16:10 (in 1h39m)"
    assert out[3] == "limit          window   percent  status     reset"
    assert out[4] == "-" * 78
    assert out[5] == "codex          5-hour       23%  allowed    resets 16:10 (in 1h39m)"
    assert out[6].split()[:4] == ["codex", "7-day", "59%", "allowed"]
    assert out[7].split()[:5] == ["Spark", "7-day", "99%", "limit", "hit"]
    assert out[8].split()[:6] == ["code", "review", "7-day", "4%", "limit", "hit"]
    assert out[9] == "plan prolite · credits 0 · reset credits 2 available"


def test_code_review_is_detail_only(monkeypatch, capsys):
    raw = dict(ENDPOINT, code_review_rate_limit={"primary_window": dict(WEEKLY, used_percent=100)})
    monkeypatch.setattr(cli, "usage", lambda **kwargs: report.shape(raw, "endpoint"))
    assert cli.main(["--warn"]) == 0
    assert "code review" not in capsys.readouterr().out
    assert cli.main(["--all", "--format", "short"]) == 0
    assert capsys.readouterr().out == "7d 59%\n"


@pytest.mark.parametrize("raw,source,expected", [
    (ENDPOINT, "endpoint", "7d 59%"),
    (WITH_SESSION, "endpoint", "5h 23% · 7d 59%"),
    (dict(ENDPOINT, model="ignored"), "endpoint", "7d 59%"),
    (SESSION, "session log, 0 min ago", "gpt-6.2-sol · 5h 23% · 7d 59%"),
    ({"primary": {"window_minutes": 10080, "used_percent": 0}}, "session log, 0 min ago", "7d 0%"),
    ({"primary": {"window_minutes": 300, "used_percent": None}}, "session log, 0 min ago", ""),
    ({}, "endpoint", ""),
])
def test_short(monkeypatch, raw, source, expected):
    monkeypatch.setattr(report.time, "time", lambda: 1794900000)
    assert report.render_short(report.shape(raw, source)) == expected


@pytest.mark.parametrize("value,code", [(69, 0), (70, 1), (71, 1), (None, 0)])
@pytest.mark.parametrize("flag,seconds", [
    ("--warn", 18000), ("--warn", 604800), ("--warn-7d", 604800),
])
def test_warn_default(monkeypatch, capsys, value, code, flag, seconds):
    raw = {"rate_limit": {"primary_window": {"limit_window_seconds": seconds, "used_percent": value}}}
    monkeypatch.setattr(cli, "usage", lambda **kwargs: report.shape(raw, "endpoint"))
    assert cli.main([flag]) == code
    assert ("5-hour:" if seconds == 18000 else "7-day:") in capsys.readouterr().out


def test_thresholds_combine_and_json(capsys):
    assert cli.main(["--warn", "60", "--warn-7d", "59", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == ENDPOINT
    assert cli.main(["--warn", "59", "--warn-7d", "60", "--format", "short"]) == 1
    assert capsys.readouterr().out == "7d 59%\n"
    assert cli.main(["--warn", "60", "--warn-7d", "60"]) == 0


def test_missing_weekly_never_warns(monkeypatch):
    raw = {"rate_limit": {"primary_window": FIVE_HOUR}}
    monkeypatch.setattr(cli, "usage", lambda **kwargs: report.shape(raw, "endpoint"))
    assert cli.main(["--warn-7d", "0"]) == 0


@pytest.mark.parametrize("args", [["--json"], ["--format", "json"], ["--all", "--json"]])
def test_json_preserves_payload_without_identities(args, monkeypatch, capsys):
    raw = dict(ENDPOINT, email="private@example.invalid", user_id="private", account_id="private")
    monkeypatch.setattr(cli, "usage", lambda **kwargs: report.shape(raw, "endpoint"))
    assert cli.main(args) == 0
    assert capsys.readouterr().out == json.dumps(ENDPOINT, indent=1) + '\n'


def test_last_format_flag_wins(capsys):
    assert cli.main(["--json", "--format", "short"]) == 0
    assert capsys.readouterr().out == "7d 59%\n"
    assert cli.main(["--format", "short", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ENDPOINT


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "codex-usage 0.1.0\n"


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--json", "--warn", "--warn-7d", "--all", "--format", "--verbose", "--version"):
        assert flag in out
    assert '--statusline' not in out


def test_argparse_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--format", "bad"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_malformed_windows_are_skipped_not_fatal():
    out = report.shape({'rate_limit': {
        'primary_window': {'used_percent': 5, 'limit_window_seconds': None},
        'secondary_window': {'used_percent': 9, 'limit_window_seconds': 604800, 'reset_at': None}},
        'additional_rate_limits': [{'rate_limit': {'primary_window': {
            'used_percent': 1, 'limit_window_seconds': 604800}}}]}, 'endpoint')
    assert [w['label'] for w in out['windows']] == ['7-day', '7-day other']
