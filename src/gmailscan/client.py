"""Search and decode Gmail messages.

Auth lives in :mod:`gmailscan.auth`. Nothing here knows about recruiters,
shipments or purchases; parsing stays in the project that cares.

:func:`clients` builds one :class:`GmailClient` per authorized mailbox. A
project that wants a single mailbox passes ``accounts=[...]`` or sets
``GMAILSCAN_ACCOUNTS``. A message present in two mailboxes is reported twice;
deduplication is the caller's.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Container, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .auth import (
    SETUP_HINT,
    GmailAuthRequired,
    GmailUnavailable,
    Health,
    authorized_accounts,
    health,
    load_credentials,
)

log = logging.getLogger(__name__)

__all__ = [
    "EmailMessage",
    "GmailClient",
    "MessageRef",
    "clients",
    "decode_message",
]


@dataclass(frozen=True)
class EmailMessage:
    """One decoded Gmail message.

    There is no ``body`` property. Callers say which part they want:
    :attr:`text_first` for recruiter prose, :attr:`html_first` for order
    tables. Each falls back to the other part.
    """

    # Four required fields. The rest default so a hand-built fixture names
    # only what it uses. Field order is part of the API (positional callers).
    id: str
    subject: str
    sender: str
    date: str  # the Date header, e.g. "Mon, 24 Aug 2026 10:02:11 -0400"
    text: str | None = None
    html: str | None = None
    threadId: str = ""
    to: str = ""  # for sent mail, the other party
    account: str = ""  # which mailbox this came from
    # Gmail's receipt time (internalDate), UTC. Sort and window on this; the
    # Date header is whatever the sender's machine wrote.
    received: datetime | None = None

    @property
    def text_first(self) -> str:
        """Plain text if present, else HTML, else empty."""
        return self.text or self.html or ""

    @property
    def html_first(self) -> str:
        """HTML if present, else plain text, else empty."""
        return self.html or self.text or ""


@dataclass(frozen=True)
class MessageRef:
    """A message as ``messages.list`` returns it: id and thread id only.

    Listing costs 5 quota units per call for up to 500 of these; reading a
    message costs 20.
    """

    id: str
    threadId: str  # noqa: N815 - Gmail's own field name, as on EmailMessage


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
        # Built lazily; tests inject a fake, and construction touches nothing.
        self._service = service

        # One line in the consumer's own log says which release it is running.
        from . import __version__

        log.info("gmailscan %s reading %s", __version__, self.account)

    def token_file(self) -> Path:
        """Where this client's token lives.

        Override it in a subclass to keep honouring a token setting that
        predates gmailscan; :attr:`service` and :meth:`health` both read the
        file it returns.
        """
        from .auth import token_path

        return token_path(self.account)

    def health(self) -> Health:
        """:func:`gmailscan.auth.health` for this mailbox, via :meth:`token_file`."""
        return health(self.account, path=self.token_file())

    @property
    def service(self) -> Any:
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover - exercised by the import guard test
                raise GmailUnavailable(
                    "google-api-python-client is not installed; install gmailscan's dependencies."
                ) from exc

            creds = self._credentials or load_credentials(self.account, path=self.token_file())
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def list_ids(
        self,
        query: str,
        *,
        after: date | None = None,
        before: date | None = None,
        limit: int = 500,
    ) -> list[MessageRef]:
        """The ids of every message matching a query, none of them read.

        5 quota units per call for up to 500 ids, against 20 to read one
        message. List, drop the ids already processed, then
        :meth:`get_message` the rest. The ``threadId`` on each ref also says
        which threads had mail in the window.
        """
        refs: list[MessageRef] = []
        for ref in self._refs(_windowed(query, after, before), lambda: min(500, limit - len(refs))):
            refs.append(ref)
            if len(refs) >= limit:
                break
        return refs

    def get_message(self, message_id: str, *, headers_only: bool = False) -> EmailMessage:
        """One message by id, decoded."""
        return decode_message(self._read(message_id, headers_only), account=self.account)

    def search(
        self,
        query: str,
        *,
        after: date | None = None,
        before: date | None = None,
        limit: int = 200,
        headers_only: bool = False,
        skip_ids: Container[str] | None = None,
    ) -> Iterator[EmailMessage]:
        """Yield decoded messages matching a Gmail search query.

        ``after`` and ``before`` become Gmail's own date operators, so the
        window is applied server-side; both are exclusive, in the mailbox's
        time zone. ``limit`` is a hard stop per call, since a first sweep would
        otherwise walk years of history.

        ``skip_ids`` holds ids already processed: they are listed but not read,
        and do not count toward ``limit``. Any container works, including one
        whose ``__contains__`` asks a database.

        ``headers_only`` fetches ``format=metadata`` (From, To, Subject, Date)
        and leaves ``text`` and ``html`` as ``None``. It saves bandwidth, not
        quota: Gmail charges a metadata get the same 20 units as a full one.
        """
        query = _windowed(query, after, before)
        fetched = 0
        skipped = 0

        # Without skip_ids, list only as many ids as will be read. With it, how
        # many of a page are new is unknown, so take full pages; a list call
        # costs the same 5 units for one id or 500.
        def page_size() -> int:
            return 500 if skip_ids is not None else min(500, limit - fetched)

        for ref in self._refs(query, page_size):
            if fetched >= limit:
                break
            if skip_ids is not None and ref.id in skip_ids:
                skipped += 1
                continue
            fetched += 1
            yield decode_message(self._read(ref.id, headers_only), account=self.account)
        if skip_ids is not None:
            log.debug("search %r: read %d, skipped %d already seen", query, fetched, skipped)

    def _refs(self, query: str, page_size: Callable[[], int]) -> Iterator[MessageRef]:
        """Walk messages.list one page per call, through the backoff.

        ``page_size`` is asked before each call; the walk stops when it returns
        zero or Gmail has no next page.
        """
        messages = self.service.users().messages()
        page_token: str | None = None
        while (size := page_size()) > 0:
            response = _with_backoff(
                messages.list(userId="me", q=query, pageToken=page_token, maxResults=size)
            )
            for stub in response.get("messages", []):
                yield MessageRef(id=str(stub["id"]), threadId=str(stub.get("threadId", "")))
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def _read(self, message_id: str, headers_only: bool) -> dict[str, Any]:
        """One messages.get, through the backoff, in the format asked for."""
        messages = self.service.users().messages()
        if headers_only:
            request = messages.get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
        else:
            request = messages.get(userId="me", id=message_id, format="full")
        payload: dict[str, Any] = _with_backoff(request)
        return payload

    def get_thread(self, thread_id: str) -> list[EmailMessage]:
        """Every message in a thread, oldest first as Gmail returns them."""
        if not thread_id:
            return []
        payload = _with_backoff(
            self.service.users().threads().get(userId="me", id=thread_id, format="full")
        )
        return [
            decode_message(message, account=self.account)
            for message in payload.get("messages") or []
        ]

    def raw(self, message_id: str) -> bytes:
        """The full RFC 822 message, for dumping a fixture or debugging a parse."""
        response = _with_backoff(
            self.service.users().messages().get(userId="me", id=message_id, format="raw")
        )
        return _b64decode(str(response["raw"]))


def _with_backoff(request: Any, *, attempts: int = 9) -> Any:
    """Execute a Gmail request, waiting out the per-minute quota.

    Gmail meters quota per user per minute, so the schedule has to be able to
    outlast a full minute: nine attempts with full jitter, each wait capped at
    90 s, and Google's ``Retry-After`` used when present (clamped to 120 s).
    Only rate limits (429, and the 403 that says ``rateLimit`` or
    ``quotaExceeded``) and 5xx responses are retried. Anything else, including
    the 403 for a revoked grant or the wrong scope, is raised at once.
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

            # Prefer Google's own Retry-After, clamped so a bad header cannot
            # stall the run.
            retry_after = None
            try:
                raw = exc.resp.get("retry-after") if hasattr(exc.resp, "get") else None
                retry_after = min(120.0, float(raw)) if raw else None
            except (TypeError, ValueError):
                retry_after = None

            # Full jitter, so two jobs on one mailbox do not retry in lockstep.
            delay = retry_after or min(90.0, 2.0**attempt) * (0.5 + random.random() / 2)
            log.warning(
                "Gmail rate limit (attempt %d/%d); retrying in %.1fs",
                attempt + 1,
                attempts,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover - the loop returns or raises


def clients(accounts: list[str] | None = None) -> list[GmailClient]:
    """One client per mailbox: every authorized one, or those asked for.

    Raises :class:`GmailAuthRequired` when nothing is authorized or when a
    requested account has no token.
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
    before: date | None = None,
    limit: int = 200,
    headers_only: bool = False,
    skip_ids: Container[str] | None = None,
) -> Iterator[EmailMessage]:
    """Run one query across every mailbox, tagging each hit with its account.

    ``limit`` applies per mailbox. A mailbox whose grant has died is logged and
    skipped so the others are still read; when every mailbox fails,
    :class:`GmailAuthRequired` is raised.
    """
    targets = clients(accounts)
    failures: list[str] = []
    for client in targets:
        try:
            yield from client.search(
                query,
                after=after,
                before=before,
                limit=limit,
                headers_only=headers_only,
                skip_ids=skip_ids,
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
        received=_received(payload.get("internalDate")),
    )


def _windowed(query: str, after: date | None, before: date | None) -> str:
    """Append Gmail's own date operators, so the window is applied server-side."""
    if after is not None:
        query = f"{query} after:{after.strftime('%Y/%m/%d')}"
    if before is not None:
        query = f"{query} before:{before.strftime('%Y/%m/%d')}"
    return query


def _received(internal_date: Any) -> datetime | None:
    """``internalDate`` is epoch milliseconds as a string; absent on a fixture."""
    try:
        return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


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

    The account is part of the name because the same message id can appear in
    two mailboxes.
    """
    safe_account = account.replace("@", "_at_").replace("/", "_")
    return dump_dir / f"{source}-{safe_account}-{message_id}.eml"
