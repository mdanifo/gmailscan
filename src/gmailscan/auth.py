"""Where Gmail tokens live, and how they become usable credentials.

**One canonical store**, ``~/.config/google-oauth/token-<address>.json``. Before
this package there were three: that path, ``virtual-closet/.sessions/``, and
whatever ``AMAZON_LEDGER_GMAIL_TOKEN`` / ``JOBPIPE_GMAIL_TOKEN`` pointed at. Two
projects resolved the third by reaching into a sibling repo's working directory,
which is not something you can install, pin, or deploy -- and the copies had
already drifted, so the fallback could serve a token weeks older than the good
one.

Resolution order, first hit wins:

1. ``GMAILSCAN_TOKEN`` -- one explicit file, for a single-account caller.
2. ``GMAILSCAN_TOKEN_DIR`` -- a directory of ``token-<address>.json``. This is
   how an unattended Lambda points at a writable temp dir it hydrated from
   Secrets Manager (see :mod:`gmailscan.secrets`).
3. ``~/.config/google-oauth/token-<address>.json`` -- the canonical store.

The scope is ``gmail.readonly`` and nothing else. This package can search and
read mail; it cannot send, modify, label, or delete it. That is structural
rather than a promise, and :mod:`tests.test_auth` pins it.

No consent flow runs here. A grant is made once by ``gmailscan-auth`` and cached
as a token file; if the file is missing or its refresh is rejected, this raises
:class:`GmailAuthRequired` telling the human what to run. An unattended timer
must never block on a browser.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)

TOKEN_PREFIX = "token-"
TOKEN_SUFFIX = ".json"

#: The canonical token directory. Everything else is an override.
DEFAULT_TOKEN_DIR = Path.home() / ".config" / "google-oauth"

SETUP_HINT = (
    "Authorize the mailbox once with: gmailscan-auth --account <address>. "
    "If this recurs every ~7 days, the Google OAuth consent screen is still in "
    "Testing mode -- publish it to Production, because Testing-mode refresh "
    "tokens expire after 7 days and no amount of re-authorizing outlasts that."
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
    """Where ``account``'s token is cached (e.g. ``"mdanifo@gmail.com"``)."""
    explicit = os.environ.get("GMAILSCAN_TOKEN", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return token_dir() / f"{TOKEN_PREFIX}{account.strip().lower()}{TOKEN_SUFFIX}"


def authorized_accounts() -> list[str]:
    """Every Gmail address with a cached token, sorted.

    This is what makes "scan both mailboxes" the default rather than a thing
    each project reimplements. ``GMAILSCAN_ACCOUNTS`` pins the list instead --
    a project that deliberately reads one mailbox sets it and stops discovering.
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
    """Whether a read could run, without importing the Google libraries.

    A UI asks this to decide between offering a sync and explaining how to set
    one up, so it must not blow up on a machine that has never seen a token.
    """
    if account:
        return token_path(account).exists()
    return bool(authorized_accounts())


def persist_token(path: Path, json_text: str) -> None:
    """Write a refreshed token back, when the filesystem allows it.

    Some callers mount the grant read-only so a container cannot mutate the
    shared file. A refresh still yields a usable in-memory credential, and
    failing the whole run because that file cannot be updated would make the
    read-only mount a footgun rather than a safety measure.
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
    """Usable credentials for ``account``, refreshing and writing back if needed.

    ``path`` overrides where the token is read from, for a consumer that still
    honours its own pre-gmailscan configuration. Without it, discovery is
    :func:`token_path`.

    Every failure mode reports the fix, because the thing reading this is
    usually a log from a 3am timer with nobody watching.
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
        # google-auth ships no annotations for this constructor, so strict mode
        # sees an untyped call into a typed context.
        creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call, unused-ignore]
            str(path), list(SCOPES)
        )
    except ValueError as exc:
        # A token file with no refresh_token parses as invalid here rather than
        # failing later. Say so with the fix attached: an access token alone
        # works for an hour and then dies inside an overnight timer.
        raise GmailAuthRequired(
            f"Gmail token at {path} is not a usable authorized-user file ({exc}). {SETUP_HINT}"
        ) from exc

    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GmailAuthRequired(
                f"Gmail token refresh for {account} failed ({exc}); the grant was "
                f"revoked or expired. {SETUP_HINT}"
            ) from exc
        persist_token(path, creds.to_json())
        return creds
    raise GmailAuthRequired(
        f"Gmail token at {path} has no refresh token, so it cannot be renewed. {SETUP_HINT}"
    )
