"""Resolve Codex's ChatGPT access token from local credential stores.

Priority: explicit environment override, auth.json, macOS Keychain.
Only Codex refreshes tokens. Errors and verbose notes never include secrets.
"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

KEYCHAIN_SERVICE = "Codex Auth"
AUTH_CLAIM = "https://api.openai.com/auth"
LOGIN_HELP = "run `codex login` to sign in with ChatGPT"


class CredentialsNotFound(RuntimeError):
    """Raised when no usable Codex ChatGPT access token can be located."""


def config_dir():
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def _claims(token):
    try:
        _, payload, _ = token.split('.')
        data = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _credentials(token, account_id):
    claims = _claims(token)
    expires = claims.get("exp")
    if isinstance(expires, (int, float)) and expires < time.time():
        raise CredentialsNotFound("the access token has expired; open `codex` so it refreshes")
    auth = claims.get(AUTH_CLAIM)
    if not isinstance(auth, dict):
        auth = {}
    if not isinstance(account_id, str) or not account_id:
        account_id = auth.get("chatgpt_account_id")
    plan = auth.get("chatgpt_plan_type")
    return token, account_id if isinstance(account_id, str) else None, plan if isinstance(plan, str) else None


def _from_credentials(creds):
    if not isinstance(creds, dict):
        raise CredentialsNotFound("could not read Codex credentials; " + LOGIN_HELP)
    tokens = creds.get("tokens")
    if not tokens and (creds.get("OPENAI_API_KEY") or creds.get("auth_mode") == "apikey"):
        raise CredentialsNotFound("an API-key login has no ChatGPT plan limits; " + LOGIN_HELP)
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        raise CredentialsNotFound("credentials hold no ChatGPT access token; " + LOGIN_HELP)
    return _credentials(token, tokens.get("account_id"))


def _from_keychain():
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0:
            return _from_credentials(json.loads(out.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _resolve_credentials(config):
    override = os.environ.get("CODEX_ACCESS_TOKEN")
    if override:
        return _credentials(override, os.environ.get("CODEX_ACCOUNT_ID")), "CODEX_ACCESS_TOKEN"

    path = (Path(config) if config is not None else config_dir()) / 'auth.json'
    try:
        with path.open(encoding="utf-8") as stream:
            creds = json.load(stream)
    except FileNotFoundError:
        if sys.platform == "darwin":
            keychain = _from_keychain()
            if keychain is not None:
                return keychain, "macOS keychain"
        raise CredentialsNotFound("no readable Codex auth.json; " + LOGIN_HELP) from None
    except (OSError, ValueError):
        raise CredentialsNotFound("could not read Codex auth.json; " + LOGIN_HELP) from None
    return _from_credentials(creds), "credentials file"


def resolve_credentials(config=None, verbose=False):
    """Return the access token, optional account ID, and optional plan."""
    credentials, source = _resolve_credentials(config)
    if verbose:
        print("credentials: " + source, file=sys.stderr)
    return credentials


def resolve_plan(config=None):
    """Return the raw plan from the selected credential's JWT when available."""
    try:
        _, _, plan = resolve_credentials(config)
    except CredentialsNotFound:
        return None
    return plan
