import base64
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from codex_usage import api, auth


@pytest.fixture(autouse=True)
def local_only(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(api, "_get_json", lambda *a, **k: pytest.fail("unmocked HTTP call"))


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


def credentials(tmp_path, **overrides):
    tokens = {"access_token": "PRIVATE-TOKEN", "refresh_token": "PRIVATE-REFRESH",
              "account_id": "PRIVATE-ACCOUNT"}
    tokens.update(overrides)
    (tmp_path / 'auth.json').write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}))


def test_env_wins_as_is(monkeypatch, tmp_path, capsys):
    credentials(tmp_path, access_token=jwt(exp=1))
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", " token as-is ")
    monkeypatch.setenv("CODEX_ACCOUNT_ID", "override-account")
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: pytest.fail("keychain called"))
    assert auth.resolve_credentials(verbose=True) == (" token as-is ", "override-account", None)
    assert capsys.readouterr().err == "credentials: CODEX_ACCESS_TOKEN\n"


def test_override_uses_jwt_claims(monkeypatch):
    token = jwt(exp=time.time() + 3600, **{auth.AUTH_CLAIM: {
        "chatgpt_account_id": "claim-account", "chatgpt_plan_type": "prolite",
    }})
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", token)
    assert auth.resolve_credentials() == (token, "claim-account", "prolite")
    monkeypatch.setenv("CODEX_ACCOUNT_ID", "override-account")
    assert auth.resolve_credentials() == (token, "override-account", "prolite")


def test_file_before_keychain(monkeypatch, tmp_path, capsys):
    credentials(tmp_path)
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: pytest.fail("keychain called"))
    assert auth.resolve_credentials(verbose=True) == ("PRIVATE-TOKEN", "PRIVATE-ACCOUNT", None)
    assert capsys.readouterr().err == "credentials: credentials file\n"


def test_file_account_id_wins_over_claim(tmp_path):
    token = jwt(**{auth.AUTH_CLAIM: {"chatgpt_account_id": "claim-account", "chatgpt_plan_type": "pro"}})
    credentials(tmp_path, access_token=token)
    assert auth.resolve_credentials() == (token, "PRIVATE-ACCOUNT", "pro")
    credentials(tmp_path, access_token=token, account_id=None)
    assert auth.resolve_credentials() == (token, "claim-account", "pro")


@pytest.mark.parametrize("source", ["env", "file", "keychain"])
def test_expired_jwt_rejected(monkeypatch, tmp_path, capsys, source):
    token = jwt(exp=time.time() - 10)
    if source == "env":
        monkeypatch.setenv("CODEX_ACCESS_TOKEN", token)
    elif source == "file":
        credentials(tmp_path, access_token=token)
    else:
        monkeypatch.setattr(auth.sys, "platform", "darwin")
        monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps({"tokens": {"access_token": token}}),
        ))
    with pytest.raises(auth.CredentialsNotFound, match="open `codex` so it refreshes") as exc:
        auth.resolve_credentials(verbose=True)
    assert token not in str(exc.value) + capsys.readouterr().err


def test_expiry_equal_to_now_is_accepted(monkeypatch):
    monkeypatch.setattr(auth.time, 'time', lambda: 1792000000)
    token = jwt(exp=1792000000)
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", token)
    assert auth.resolve_credentials() == (token, None, None)


@pytest.mark.parametrize("token", ["unverified", "header.abc.signature", jwt(exp="invalid")])
def test_undecodable_expiry_is_ignored(monkeypatch, token):
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", token)
    assert auth.resolve_credentials() == (token, None, None)


def test_api_key_login_is_helpful(monkeypatch, tmp_path, capsys):
    (tmp_path / 'auth.json').write_text(json.dumps({
        "auth_mode": "apikey", "OPENAI_API_KEY": "PRIVATE-KEY", "tokens": None,
    }))
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: pytest.fail("keychain called"))
    with pytest.raises(auth.CredentialsNotFound) as exc:
        auth.resolve_credentials(verbose=True)
    assert str(exc.value) == ("an API-key login has no ChatGPT plan limits; "
                              "run `codex login` to sign in with ChatGPT")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("raw", ['PRIVATE-TOKEN', '"PRIVATE-TOKEN"',
                                 '{"tokens": "PRIVATE-TOKEN"}',
                                 '{"tokens": {"refresh_token": "PRIVATE-REFRESH"}}'])
def test_bad_credentials_never_leak_or_try_keychain(monkeypatch, tmp_path, raw, capsys):
    (tmp_path / 'auth.json').write_text(raw)
    monkeypatch.setattr(auth.sys, "platform", "darwin")
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: pytest.fail("keychain called"))
    with pytest.raises(auth.CredentialsNotFound, match="run `codex login`") as exc:
        auth.resolve_credentials(verbose=True)
    assert "PRIVATE" not in str(exc.value) + capsys.readouterr().err


def test_keychain_fallback(monkeypatch, capsys):
    monkeypatch.setattr(auth.sys, "platform", "darwin")

    def run(command, **kwargs):
        assert command == ["security", "find-generic-password", "-s", "Codex Auth", "-w"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 15}
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "tokens": {"access_token": "keychain-token", "account_id": "keychain-account"},
        }))

    monkeypatch.setattr(auth.subprocess, "run", run)
    assert auth.resolve_credentials(verbose=True) == ("keychain-token", "keychain-account", None)
    assert capsys.readouterr().err == "credentials: macOS keychain\n"


@pytest.mark.parametrize("failure", ["unavailable", "missing", "malformed", "timeout"])
def test_keychain_failure_is_safe(monkeypatch, failure, capsys):
    monkeypatch.setattr(auth.sys, "platform", "darwin")

    def run(*args, **kwargs):
        if failure == "unavailable":
            raise OSError("PRIVATE-TOKEN")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("PRIVATE-TOKEN", 15)
        return SimpleNamespace(returncode=int(failure == "missing"), stdout="PRIVATE-TOKEN")

    monkeypatch.setattr(auth.subprocess, "run", run)
    with pytest.raises(auth.CredentialsNotFound, match="no readable Codex auth.json") as exc:
        auth.resolve_credentials(verbose=True)
    assert "PRIVATE" not in str(exc.value) + capsys.readouterr().err


def test_missing_file_off_macos_skips_keychain(monkeypatch):
    monkeypatch.setattr(auth.subprocess, "run", lambda *a, **k: pytest.fail("keychain called"))
    with pytest.raises(auth.CredentialsNotFound, match="run `codex login`"):
        auth.resolve_credentials()


def test_empty_config_dir_uses_default(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", "")
    monkeypatch.setattr(auth.os.path, "expanduser", lambda path: str(tmp_path / ".codex"))
    assert auth.config_dir() == tmp_path / '.codex'
