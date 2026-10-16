"""Token files and credentials.

Tokens are cached per account as ``token-<address>.json``. Lookup order, first
hit wins:

1. ``GMAILSCAN_TOKEN``: one explicit file, for a single-account caller.
2. ``GMAILSCAN_TOKEN_DIR``: a directory of ``token-<address>.json``. An
   unattended run points this at a writable directory it hydrated from
   Secrets Manager (see :mod:`gmailscan.secrets`).
3. ``~/.config/google-oauth/``, the default store.

The scope is ``gmail.readonly``; ``tests/test_auth.py`` pins it.

No consent flow runs here. ``gmailscan-auth`` makes the grant once and caches
it as a token file. When the file is missing or the refresh is rejected,
:class:`GmailAuthRequired` says what to run.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)

TOKEN_PREFIX = "token-"
TOKEN_SUFFIX = ".json"

#: The default token directory. The environment variables above override it.
DEFAULT_TOKEN_DIR = Path.home() / ".config" / "google-oauth"

SETUP_HINT = (
    "Authorize the mailbox once with: gmailscan-auth --account <address>. "
    "If this recurs every ~7 days, the Google OAuth consent screen is still in "
    "Testing mode: publish it to Production. Testing-mode refresh tokens expire "
    "after 7 days, and re-authorizing does not change that."
)


class GmailAuthRequired(RuntimeError):
    """There is no usable Gmail token for this account on this machine."""


class GmailUnavailable(RuntimeError):
    """The Google client libraries are not installed."""


def token_dir() -> Path:
    """The directory holding per-account tokens."""
    override = os.environ.get("GMAILSCAN_TOKEN_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_TOKEN_DIR


def token_path(account: str) -> Path:
    """Where ``account``'s token is cached (e.g. ``"you@gmail.com"``)."""
    explicit = os.environ.get("GMAILSCAN_TOKEN", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return token_dir() / f"{TOKEN_PREFIX}{account.strip().lower()}{TOKEN_SUFFIX}"


def authorized_accounts() -> list[str]:
    """Every Gmail address with a cached token, sorted.

    ``GMAILSCAN_ACCOUNTS`` (comma-separated) pins the list instead of
    discovering it; set it when a project should read one mailbox only.
    """
    pinned = os.environ.get("GMAILSCAN_ACCOUNTS", "").strip()
    if pinned:
        return sorted({a.strip().lower() for a in pinned.split(",") if a.strip()})

    directory = token_dir()
    if not directory.exists():
        return []
    return sorted(
        p.name[len(TOKEN_PREFIX) : -len(TOKEN_SUFFIX)].lower()
        for p in directory.glob(f"{TOKEN_PREFIX}*{TOKEN_SUFFIX}")
    )


def is_configured(account: str | None = None) -> bool:
    """Whether a token file exists, without importing the Google libraries.

    Safe to call from a UI on a machine that has never seen a token. A revoked
    grant leaves its file behind, so this says nothing about whether the grant
    still works; :func:`health` does.
    """
    if account:
        return token_path(account).exists()
    return bool(authorized_accounts())


Health = Literal["missing", "configured", "expired", "revoked", "ok"]


def health(account: str, *, path: Path | None = None) -> Health:
    """Whether ``account``'s grant works, refreshing it to find out.

    - ``"missing"``: no token file.
    - ``"configured"``: a token exists but could not be checked (google-auth
      not installed, no network, or a retryable server error). Not a reason
      to re-authorize.
    - ``"expired"``: the file cannot renew itself: no refresh token, or not an
      authorized-user file at all.
    - ``"revoked"``: Google refused the refresh. Revocation and a Testing-mode
      grant past its 7 days both come back as ``invalid_grant``.
    - ``"ok"``: usable now. A refreshed token is written back.

    Never raises. ``path`` overrides discovery, as for :func:`load_credentials`.
    """
    return health_detail(account, path=path)[0]


def health_detail(account: str, *, path: Path | None = None) -> tuple[Health, str]:
    """:func:`health` plus one line saying why, for a status screen or a log.

    The detail is for a person (the missing path, Google's error text,
    ``"refreshed"`` or ``"valid"``). Branch on the state, not the detail.
    """
    path = path or token_path(account)
    if not path.exists():
        return "missing", f"no token file at {path}"
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return "configured", "google-auth is not installed, so the grant cannot be checked"

    try:
        creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call, unused-ignore]
            str(path), list(SCOPES)
        )
    except ValueError as exc:
        return "expired", f"not a usable authorized-user file ({exc})"
    except OSError as exc:
        return "configured", f"token file is not readable ({exc})"
    if not creds.refresh_token:
        return "expired", "no refresh token, so it cannot be renewed"
    if creds.valid:
        return "ok", "valid"
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        if getattr(exc, "retryable", False):
            return "configured", f"Google could not answer just now ({exc})"
        return "revoked", str(exc)
    except Exception as exc:
        return "configured", f"could not reach Google ({exc})"
    persist_token(path, creds.to_json())
    return "ok", "refreshed"


def persist_token(path: Path, json_text: str) -> None:
    """Write a refreshed token back, if the filesystem allows it.

    A token mounted read-only into a container still refreshes in memory, so a
    failed write-back is logged and the run goes on.
    """
    try:
        path.write_text(json_text, encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        log.warning(
            "could not persist refreshed Gmail token at %s (%s); using it in memory",
            path,
            exc,
        )


def load_credentials(account: str, *, path: Path | None = None) -> Any:
    """Usable credentials for ``account``, refreshed and written back if needed.

    ``path`` overrides where the token is read from, for a consumer that keeps
    its own token setting; the default is :func:`token_path`. Each failure
    names the fix, since the reader is usually a log from an overnight timer.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - exercised by the import guard test
        raise GmailUnavailable(
            "google-auth is not installed; install gmailscan's dependencies."
        ) from exc

    path = path or token_path(account)
    if not path.exists():
        raise GmailAuthRequired(f"No Gmail token for {account} at {path}. {SETUP_HINT}")

    try:
        # google-auth ships no annotations for this constructor.
        creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call, unused-ignore]
            str(path), list(SCOPES)
        )
    except ValueError as exc:
        # google-auth rejects a file with no refresh_token here. Say so, with
        # the fix.
        raise GmailAuthRequired(
            f"Gmail token at {path} is not a usable authorized-user file ({exc}). {SETUP_HINT}"
        ) from exc

    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise GmailAuthRequired(
            f"Gmail token at {path} has no refresh token, so it cannot be renewed. {SETUP_HINT}"
        )
    # A file holding a refresh token but no access token (hydrated from a store
    # that kept only the grant) is neither valid nor expired. Refresh anything
    # that is not valid.
    try:
        creds.refresh(Request())
    except Exception as exc:
        raise GmailAuthRequired(
            f"Gmail token refresh for {account} failed ({exc}); the grant was "
            f"revoked or expired. {SETUP_HINT}"
        ) from exc
    persist_token(path, creds.to_json())
    return creds
