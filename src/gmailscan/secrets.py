"""Optional AWS Secrets Manager token store, for unattended runs.

Requires the ``secrets`` extra (``pip install "gmailscan[secrets]"``). ``boto3``
is imported lazily inside the functions, so importing :mod:`gmailscan` never
requires AWS credentials or the library -- only actually touching a secret does.

Why this exists: the consent flow writes a token to a machine-local file, but a
Lambda or a scheduled container has no such file and cannot open a browser to
make one. It pulls the grant from a secret instead, writes it into a writable
directory, and points ``GMAILSCAN_TOKEN_DIR`` there -- after which everything in
:mod:`gmailscan.auth` works unchanged and knows nothing about AWS.

The secret is a JSON object keyed by email address, whose values are token
documents. Keys beginning with ``_`` are metadata rather than accounts --
``_meta`` carries ``granted_at``, which is what a token-expiry reminder reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .auth import TOKEN_PREFIX, TOKEN_SUFFIX, GmailAuthRequired, token_dir

__all__ = [
    "DEFAULT_SECRET_NAME",
    "META_KEY",
    "hydrate_tokens",
    "push_tokens",
    "read_meta",
]

DEFAULT_SECRET_NAME = "gmail-tokens"

#: Metadata rather than an account. Holds ``granted_at`` for expiry reminders.
META_KEY = "_meta"


def _client(region: str | None = None) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised by the extras test
        raise GmailAuthRequired(
            'The AWS token store needs the secrets extra: pip install "gmailscan[secrets]"'
        ) from exc
    return boto3.client("secretsmanager", region_name=region or os.getenv("AWS_REGION"))


def _read_secret(secret_name: str, region: str | None) -> dict[str, Any]:
    client = _client(region)
    try:
        response = client.get_secret_value(SecretId=secret_name)
    except Exception as exc:  # ResourceNotFoundException and friends
        raise GmailAuthRequired(
            f"Could not read the '{secret_name}' secret ({exc}). Populate it with "
            "gmailscan-auth --push after authorizing each account."
        ) from exc
    try:
        parsed = json.loads(response.get("SecretString") or "{}")
    except ValueError as exc:
        raise GmailAuthRequired(f"'{secret_name}' is not valid JSON ({exc}).") from exc
    return parsed if isinstance(parsed, dict) else {}


def hydrate_tokens(
    *, secret_name: str = DEFAULT_SECRET_NAME, region: str | None = None
) -> list[str]:
    """Write every token in the secret into the token directory.

    Returns the accounts written. Raises :class:`GmailAuthRequired` when the
    secret is missing, empty, or holds only metadata, so an unattended run fails
    loudly rather than quietly scanning nothing -- a silent zero-result sweep
    looks identical to a mailbox with no new mail.
    """
    tokens = _read_secret(secret_name, region)
    if not tokens:
        raise GmailAuthRequired(f"'{secret_name}' is empty; nothing to hydrate.")

    directory = token_dir()
    directory.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for account, payload in tokens.items():
        if account.startswith("_") or not isinstance(payload, dict):
            continue
        path = directory / f"{TOKEN_PREFIX}{account.strip().lower()}{TOKEN_SUFFIX}"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        written.append(account.strip().lower())

    if not written:
        raise GmailAuthRequired(
            f"'{secret_name}' has no account tokens, only metadata keys. "
            "Re-run gmailscan-auth --push."
        )
    return sorted(written)


def push_tokens(
    accounts: list[str] | None = None,
    *,
    secret_name: str = DEFAULT_SECRET_NAME,
    region: str | None = None,
    granted_at: str | None = None,
) -> list[str]:
    """Upload local token files into the secret, for unattended runs to hydrate.

    Merges rather than replaces: pushing one account must not delete the other's
    grant from the secret, which is exactly the mistake that would leave a
    scheduled sweep reading one mailbox instead of two.
    """
    from .auth import authorized_accounts, token_path

    wanted = [a.strip().lower() for a in (accounts or authorized_accounts()) if a.strip()]
    if not wanted:
        raise GmailAuthRequired("No local Gmail tokens to push.")

    try:
        existing = _read_secret(secret_name, region)
    except GmailAuthRequired:
        existing = {}  # first push creates it

    pushed: list[str] = []
    for account in wanted:
        path = token_path(account)
        if not path.exists():
            continue
        existing[account] = json.loads(path.read_text(encoding="utf-8"))
        pushed.append(account)

    if not pushed:
        raise GmailAuthRequired("None of the requested accounts has a local token file.")

    meta = existing.get(META_KEY)
    meta = dict(meta) if isinstance(meta, dict) else {}
    if granted_at:
        meta["granted_at"] = granted_at
    if meta:
        existing[META_KEY] = meta

    client = _client(region)
    body = json.dumps(existing)
    try:
        client.put_secret_value(SecretId=secret_name, SecretString=body)
    except Exception:
        client.create_secret(Name=secret_name, SecretString=body)
    return sorted(pushed)


def read_meta(*, secret_name: str = DEFAULT_SECRET_NAME, region: str | None = None) -> dict:
    """The ``_meta`` object from the secret, or ``{}`` if absent or unreadable.

    Unlike the rest of this module this never raises: a missing reminder
    timestamp should not take down the run it is attached to.
    """
    try:
        tokens = _read_secret(secret_name, region)
    except GmailAuthRequired:
        return {}
    meta = tokens.get(META_KEY)
    return dict(meta) if isinstance(meta, dict) else {}


def token_documents() -> dict[str, Path]:
    """Local token files by account -- what :func:`push_tokens` would upload."""
    from .auth import authorized_accounts, token_path

    return {a: token_path(a) for a in authorized_accounts() if token_path(a).exists()}
