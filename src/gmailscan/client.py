"""Search and decode Gmail messages, read-only.

Auth lives next door in :mod:`gmailscan.auth`; this module knows nothing about
recruiters, shipments, or purchases. Parsing stays in the project that cares.

:func:`clients` is the multi-account entry point -- one :class:`GmailClient` per
authorized mailbox, which is what "scan mdanifo and mdanifo100" means in
practice. A project that wants a single mailbox passes ``accounts=[...]`` or
sets ``GMAILSCAN_ACCOUNTS``.

Re-reading a message that exists in two mailboxes is the caller's problem to
dedupe; this layer reports what it finds.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .auth import (
    GmailAuthRequired,
    GmailUnavailable,
    SETUP_HINT,
    authorized_accounts,
    load_credentials,
)

log = logging.getLogger(__name__)

__all__ = [
    "EmailMessage",
    "GmailClient",
    "clients",
    "decode_message",
]


@dataclass(frozen=True)
class EmailMessage:
    """One decoded Gmail message.

    There is deliberately **no** ``body`` property. The two projects this was
    extracted from disagreed about what it means, and both were right for their
    own mail: recruiter correspondence carries its detail in prose, so plain
    text is the signal and the HTML twin is the same words wrapped in markup
    that only costs tokens; order confirmations carry their detail in tables, so
    the HTML is the signal and the text part is a lossy summary.

    Silently picking one would have changed what a project reads without
    changing a line of its code, so callers say which they want:
    :attr:`text_first` or :attr:`html_first`.
    """

    # Only these four are always present. Everything below is optional because
    # not every consumer threads, and a parser building a fixture by hand should
    # not have to name fields it does not use.
    id: str
    subject: str
    sender: str
    date: str  # the Date header, e.g. "Mon, 24 Aug 2026 10:02:11 -0400"
    text: str | None = None
    html: str | None = None
    threadId: str = ""
    to: str = ""  # needed so mail you sent names the other party, not you
    account: str = ""  # which mailbox this came from, once more than one is swept

    @property
    def text_first(self) -> str:
        """Plain text if present, else HTML, else empty."""
        return self.text or self.html or ""

    @property
    def html_first(self) -> str:
        """HTML if present, else plain text, else empty."""
        return self.html or self.text or ""


class GmailClient:
    """Thin wrapper over the Gmail API v1 ``users.messages`` surface, one account."""

    def __init__(
        self,
        account: str,
        *,
        credentials: Any = None,
        service: Any = None,
    ) -> None:
        self.account = account.strip().lower()
        self._credentials = credentials
        # Built lazily so tests can inject a fake and construction never touches
        # the network or the filesystem.
        self._service = service

    def token_file(self) -> Path:
        """Where this client's token lives.

        A hook, so a consumer whose own configuration predates gmailscan can
        keep honouring it without every code path having to remember -- the
        alternative is one of discovery, ``is_configured`` and credential
        loading silently disagreeing about which file is authoritative.
        """
        from .auth import token_path

        return token_path(self.account)

    @property
    def service(self) -> Any:
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover - exercised by the import guard test
                raise GmailUnavailable(
                    "google-api-python-client is not installed; install gmailscan's "
                    "dependencies."
                ) from exc

            creds = self._credentials or load_credentials(
                self.account, path=self.token_file()
            )
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def search(
        self,
        query: str,
        *,
        after: date | None = None,
        limit: int = 200,
        headers_only: bool = False,
    ) -> Iterator[EmailMessage]:
        """Yield decoded messages matching a Gmail search query.

        ``limit`` is a hard stop rather than a page size: the first sweep of a
        mailbox with years of history would otherwise walk all of it.

        ``headers_only`` fetches ``format=metadata`` -- sender, subject, date
        and nothing else. Anything surveying a mailbox rather than parsing it
        wants this: full bodies of years of mail cost quota and bandwidth to
        download megabytes and then read one header off each. ``text`` and
        ``html`` come back None, which is why it is not the default.
        """
        if after is not None:
            query = f"{query} after:{after.strftime('%Y/%m/%d')}"

        fmt = "metadata" if headers_only else "full"
        extra = (
            {"metadataHeaders": ["From", "To", "Subject", "Date"]}
            if headers_only else {}
        )

        messages = self.service.users().messages()
        page_token: str | None = None
        fetched = 0
        while fetched < limit:
            response = _with_backoff(
                messages.list(userId="me", q=query, pageToken=page_token, maxResults=100)
            )
            for stub in response.get("messages", []):
                if fetched >= limit:
                    break
                payload = _with_backoff(
                    messages.get(userId="me", id=stub["id"], format=fmt, **extra)
                )
                fetched += 1
                yield decode_message(payload, account=self.account)
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def get_thread(self, thread_id: str) -> list[EmailMessage]:
        """Every message in a thread, oldest first as Gmail returns them.

        Used to see who spoke last and to read next steps from the conversation
        rather than from a single search hit.
        """
        if not thread_id:
            return []
        payload = (
            self.service.users().threads().get(userId="me", id=thread_id, format="full").execute()
        )
        return [
            decode_message(message, account=self.account)
            for message in payload.get("messages") or []
        ]

    def raw(self, message_id: str) -> bytes:
        """The full RFC 822 message, for dumping a fixture or debugging a parse."""
        response = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="raw")
            .execute()
        )
        return _b64decode(str(response["raw"]))


def _with_backoff(request: Any, *, attempts: int = 9) -> Any:
    """Execute a Gmail request, waiting out the per-minute quota.

    Gmail meters by "query cost" per user per MINUTE. That word is the whole
    design constraint: a backoff that gives up inside sixty seconds cannot
    outlast the window it is waiting on. The first version topped out at ~14s
    after six tries -- about 26s in total -- and died mid-scan with the quota
    about to reset.

    So: nine attempts, capped at 90s each. Worst case is several minutes of
    sleeping, which is the correct behaviour for a survey that would otherwise
    have to start over.

    Only rate-limit and transient server errors are retried. A real 403 --
    revoked grant, wrong scope -- surfaces immediately rather than after nine
    sleeps, because that error never clears on its own.
    """
    import random
    import time

    from googleapiclient.errors import HttpError

    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            detail = str(exc)
            transient = status in (429, 500, 502, 503, 504) or (
                status == 403 and ("rateLimit" in detail or "quotaExceeded" in detail)
            )
            if not transient or attempt == attempts - 1:
                raise

            # Google sometimes says exactly how long to wait. Believe it over
            # a guess, clamped so a bad header cannot stall the run for hours.
            retry_after = None
            try:
                raw = exc.resp.get("retry-after") if hasattr(exc.resp, "get") else None
                retry_after = min(120.0, float(raw)) if raw else None
            except (TypeError, ValueError):
                retry_after = None

            # Full jitter: several callers backing off in lockstep would
            # otherwise retry in lockstep and re-trip the same limit.
            delay = retry_after or min(90.0, 2.0**attempt) * (0.5 + random.random() / 2)
            log.warning(
                "Gmail rate limit (attempt %d/%d); retrying in %.1fs",
                attempt + 1, attempts, delay,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")


def clients(accounts: list[str] | None = None) -> list[GmailClient]:
    """One ready client per mailbox: every authorized one, or just those asked for.

    Raises :class:`GmailAuthRequired` when nothing is authorized, or when a
    specifically requested account has no token -- asking for a mailbox and
    silently getting nothing back is worse than an error.
    """
    available = authorized_accounts()
    if accounts is None:
        if not available:
            raise GmailAuthRequired(f"No Gmail account is authorized here. {SETUP_HINT}")
        return [GmailClient(a) for a in available]

    wanted = [a.strip().lower() for a in accounts if a.strip()]
    missing = [a for a in wanted if a not in available]
    if missing:
        raise GmailAuthRequired(
            f"No Gmail token for: {', '.join(missing)}. "
            f"Authorized here: {', '.join(available) or 'none'}. {SETUP_HINT}"
        )
    return [GmailClient(a) for a in wanted]


def search_all(
    query: str,
    *,
    accounts: list[str] | None = None,
    after: date | None = None,
    limit: int = 200,
    headers_only: bool = False,
) -> Iterator[EmailMessage]:
    """Run one query across every mailbox, tagging each hit with its account.

    ``limit`` applies per mailbox, not in total, so adding an account never
    silently truncates the results from the ones already being read.

    **A mailbox whose grant has died does not abort the others.** Tokens expire
    per account -- with the consent screen in Testing they expire every 7 days,
    and in practice one address goes stale while the other is fine. Aborting the
    whole sweep on the first bad one means a dead mailbox blinds you to a
    healthy one, so each failure is logged and skipped.

    If *every* mailbox fails, that is raised: a sweep that reads nothing at all
    must not look like a sweep that found nothing.
    """
    targets = clients(accounts)
    failures: list[str] = []
    for client in targets:
        try:
            yield from client.search(
                query, after=after, limit=limit, headers_only=headers_only
            )
        except GmailAuthRequired as exc:
            failures.append(client.account)
            log.warning("skipping %s: %s", client.account, exc)
    if failures and len(failures) == len(targets):
        raise GmailAuthRequired(
            f"Every mailbox failed to authorize ({', '.join(failures)}). {SETUP_HINT}"
        )


def decode_message(payload: dict[str, Any], *, account: str = "") -> EmailMessage:
    """Turn a ``users.messages.get(format="full")`` response into an EmailMessage."""
    part = payload.get("payload") or {}
    headers = {
        str(h.get("name", "")).lower(): str(h.get("value", "")) for h in part.get("headers") or []
    }
    bodies: dict[str, str] = {}
    _collect_bodies(part, bodies)
    return EmailMessage(
        id=str(payload.get("id", "")),
        threadId=str(payload.get("threadId", "")),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        to=headers.get("to", ""),
        date=headers.get("date", ""),
        text=bodies.get("text/plain"),
        html=bodies.get("text/html"),
        account=account,
    )


def _collect_bodies(part: dict[str, Any], bodies: dict[str, str]) -> None:
    """Walk the MIME tree depth-first, keeping the first body seen per type."""
    mime = str(part.get("mimeType", ""))
    data = (part.get("body") or {}).get("data")
    if data and mime in ("text/plain", "text/html") and mime not in bodies:
        bodies[mime] = _b64decode(str(data)).decode("utf-8", errors="replace")
    for child in part.get("parts") or []:
        _collect_bodies(child, bodies)


def _b64decode(data: str) -> bytes:
    """Gmail uses URL-safe base64, occasionally without padding."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def dump_path(dump_dir: Path, source: str, account: str, message_id: str) -> Path:
    """A stable filename for a dumped message, unique across mailboxes.

    The account is in the name because the same message id can appear in two
    mailboxes and one dump must not overwrite the other.
    """
    safe_account = account.replace("@", "_at_").replace("/", "_")
    return dump_dir / f"{source}-{safe_account}-{message_id}.eml"
