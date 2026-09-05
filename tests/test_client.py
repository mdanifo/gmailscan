"""Decoding and multi-account search, with the Gmail API faked entirely."""

from __future__ import annotations

import base64
import json
from datetime import date

import pytest

from gmailscan import (
    EmailMessage,
    GmailAuthRequired,
    GmailClient,
    clients,
    decode_message,
    dump_path,
    search_all,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("GMAILSCAN_TOKEN", "GMAILSCAN_TOKEN_DIR", "GMAILSCAN_ACCOUNTS"):
        monkeypatch.delenv(key, raising=False)
    yield


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _payload(msg_id="m1", thread="t1", text="hello", html="<p>hello</p>", headers=None):
    hdrs = headers or {
        "Subject": "Your order",
        "From": "shop@example.com",
        "To": "me@example.com",
        "Date": "Mon, 24 Aug 2026 10:02:11 -0400",
    }
    parts = []
    if text is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(text)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {
        "id": msg_id,
        "threadId": thread,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": k, "value": v} for k, v in hdrs.items()],
            "parts": parts,
        },
    }


class _FakeMessages:
    def __init__(self, payloads):
        self._payloads = {p["id"]: p for p in payloads}

    def list(self, **_kw):
        ids = [{"id": i} for i in self._payloads]
        return _FakeExec({"messages": ids})

    def get(self, *, userId, id, format):  # noqa: A002 - mirrors the Google signature
        if format == "raw":
            return _FakeExec({"raw": _b64("From: a@b\r\n\r\nbody")})
        return _FakeExec(self._payloads[id])


class _FakeExec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeService:
    def __init__(self, payloads):
        self._messages = _FakeMessages(payloads)

    def users(self):
        return self

    def messages(self):
        return self._messages


# ---------------------------------------------------------------- decoding


def test_decode_extracts_headers_and_both_bodies():
    msg = decode_message(_payload(), account="a@gmail.com")
    assert msg.id == "m1"
    assert msg.threadId == "t1"
    assert msg.subject == "Your order"
    assert msg.sender == "shop@example.com"
    assert msg.to == "me@example.com"
    assert msg.text == "hello"
    assert msg.html == "<p>hello</p>"
    assert msg.account == "a@gmail.com"


def test_decode_tolerates_unpadded_base64():
    """Gmail's URL-safe base64 arrives without padding often enough to matter."""
    payload = _payload(text="a" * 5, html=None)
    assert decode_message(payload).text == "aaaaa"


def test_decode_walks_nested_mime_trees():
    nested = {
        "id": "m2",
        "threadId": "t2",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "s"}],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("deep")}},
                    ],
                }
            ],
        },
    }
    assert decode_message(nested).text == "deep"


def test_decode_survives_a_message_with_no_body():
    msg = decode_message({"id": "m3", "payload": {"headers": []}})
    assert msg.text is None and msg.html is None
    assert msg.text_first == "" and msg.html_first == ""


# ------------------------------------------------- the body-preference split


def test_no_ambiguous_body_attribute():
    """The two source projects disagreed about what `body` meant.

    open-job-aggregator preferred plain text (recruiter prose); virtual-closet
    preferred HTML (order tables). Shipping either as `body` would silently
    change what one of them reads, so neither is shipped.
    """
    assert not hasattr(EmailMessage(id="1", subject="s", sender="f", date="d"), "body")


def test_text_first_and_html_first_are_explicit():
    both = decode_message(_payload(text="plain", html="<b>rich</b>"))
    assert both.text_first == "plain"
    assert both.html_first == "<b>rich</b>"

    text_only = decode_message(_payload(text="plain", html=None))
    assert text_only.html_first == "plain"  # falls back rather than returning ""

    html_only = decode_message(_payload(text=None, html="<b>rich</b>"))
    assert html_only.text_first == "<b>rich</b>"


# ------------------------------------------------------------------ search


def test_search_yields_decoded_messages():
    client = GmailClient("a@gmail.com", service=_FakeService([_payload()]))
    found = list(client.search("from:shop@example.com"))
    assert [m.subject for m in found] == ["Your order"]
    assert found[0].account == "a@gmail.com"


def test_search_applies_the_after_filter(monkeypatch):
    captured = {}

    class _Recording(_FakeMessages):
        def list(self, **kw):
            captured.update(kw)
            return super().list(**kw)

    service = _FakeService([_payload()])
    service._messages = _Recording([_payload()])
    client = GmailClient("a@gmail.com", service=service)
    list(client.search("subject:x", after=date(2026, 8, 1)))
    assert "after:2026/08/01" in captured["q"]


def test_search_limit_is_a_hard_stop():
    """Without this the first sweep walks years of history."""
    payloads = [_payload(msg_id=f"m{i}") for i in range(10)]
    client = GmailClient("a@gmail.com", service=_FakeService(payloads))
    assert len(list(client.search("x", limit=3))) == 3


def test_raw_returns_rfc822_bytes():
    client = GmailClient("a@gmail.com", service=_FakeService([_payload()]))
    assert b"From: a@b" in client.raw("m1")


# ----------------------------------------------------------- multi-account


def _write_token(directory, account):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"token-{account}.json").write_text(
        json.dumps({"refresh_token": "r", "client_id": "c"})
    )


def test_clients_covers_every_authorized_mailbox(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "mdanifo@gmail.com")
    _write_token(tmp_path, "mdanifo100@gmail.com")
    assert [c.account for c in clients()] == ["mdanifo100@gmail.com", "mdanifo@gmail.com"]


def test_clients_can_be_pinned_to_one(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    _write_token(tmp_path, "b@gmail.com")
    assert [c.account for c in clients(["b@gmail.com"])] == ["b@gmail.com"]


def test_requesting_an_unauthorized_account_raises(monkeypatch, tmp_path):
    """Asking for a mailbox and silently getting nothing is worse than an error."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    with pytest.raises(GmailAuthRequired) as excinfo:
        clients(["missing@gmail.com"])
    assert "missing@gmail.com" in str(excinfo.value)
    assert "a@gmail.com" in str(excinfo.value)  # says what IS available


def test_no_accounts_at_all_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    with pytest.raises(GmailAuthRequired):
        clients()


def test_search_all_tags_each_hit_with_its_mailbox(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    _write_token(tmp_path, "b@gmail.com")
    monkeypatch.setattr(
        "gmailscan.client.GmailClient.service",
        property(lambda self: _FakeService([_payload()])),
    )
    accounts = [m.account for m in search_all("x")]
    assert sorted(accounts) == ["a@gmail.com", "b@gmail.com"]


def test_dump_path_is_unique_per_mailbox():
    """The same message id can exist in both mailboxes; one dump must not
    overwrite the other."""
    from pathlib import Path

    a = dump_path(Path("/d"), "amazon", "mdanifo@gmail.com", "m1")
    b = dump_path(Path("/d"), "amazon", "mdanifo100@gmail.com", "m1")
    assert a != b
    assert "@" not in a.name  # safe on every filesystem


# ------------------------------------------------- one dead mailbox of several


def _dead(account):
    def _raise(self, *a, **k):
        raise GmailAuthRequired(f"refresh for {account} failed (invalid_grant)")

    return _raise


def test_a_dead_mailbox_does_not_blind_the_healthy_one(monkeypatch, tmp_path, caplog):
    """Tokens expire per account. With the consent screen in Testing they expire
    every 7 days, and in practice one address goes stale while the other is
    fine -- which is the live situation this was written in. Aborting the sweep
    on the first bad mailbox would hide the good one's mail entirely.
    """
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "alive@gmail.com")
    _write_token(tmp_path, "dead@gmail.com")

    real_search = GmailClient.search

    def search(self, query, **kw):
        if self.account == "dead@gmail.com":
            raise GmailAuthRequired("refresh failed (invalid_grant)")
        self._service = _FakeService([_payload()])
        return real_search(self, query, **kw)

    monkeypatch.setattr(GmailClient, "search", search)

    found = list(search_all("x"))
    assert [m.account for m in found] == ["alive@gmail.com"]
    assert "dead@gmail.com" in caplog.text


def test_every_mailbox_failing_still_raises(monkeypatch, tmp_path):
    """A sweep that read nothing at all must not look like one that found nothing."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    _write_token(tmp_path, "b@gmail.com")

    def always_dead(self, query, **kw):
        raise GmailAuthRequired("refresh failed (invalid_grant)")

    monkeypatch.setattr(GmailClient, "search", always_dead)

    with pytest.raises(GmailAuthRequired, match="Every mailbox"):
        list(search_all("x"))


# --------------------------------------------- quota: metadata and backoff


def test_headers_only_asks_gmail_for_metadata(monkeypatch):
    """Surveying a mailbox must not download years of bodies to read one header
    off each -- that is what exhausted the per-minute quota."""
    captured = {}

    class _Recording(_FakeMessages):
        def get(self, **kw):
            captured.update(kw)
            return _FakeExec(_payload())

    service = _FakeService([_payload()])
    service._messages = _Recording([_payload()])
    client = GmailClient("a@gmail.com", service=service)
    list(client.search("x", headers_only=True))

    assert captured["format"] == "metadata"
    assert "From" in captured["metadataHeaders"]


def test_full_fetch_is_still_the_default():
    """headers_only leaves text and html None, so parsing callers must opt out
    of it rather than into it."""
    captured = {}

    class _Recording(_FakeMessages):
        def get(self, **kw):
            captured.update(kw)
            return _FakeExec(_payload())

    service = _FakeService([_payload()])
    service._messages = _Recording([_payload()])
    list(GmailClient("a@gmail.com", service=service).search("x"))
    assert captured["format"] == "full"


def test_rate_limit_is_retried_not_raised(monkeypatch):
    """Gmail meters by query cost per user per minute and a survey reaches it in
    seconds. The 403 says rateLimitExceeded and clears on its own."""
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    monkeypatch.setattr("time.sleep", lambda _: None)

    class _Resp:
        status = 403
        reason = "Forbidden"

    calls = {"n": 0}

    class _Req:
        def execute(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise HttpError(_Resp(), b'{"error":{"message":"rateLimitExceeded"}}')
            return {"ok": True}

    assert client_mod._with_backoff(_Req()) == {"ok": True}
    assert calls["n"] == 3


def test_a_real_permission_error_is_not_retried(monkeypatch):
    """A revoked grant must surface at once, not after six sleeps."""
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    monkeypatch.setattr("time.sleep", lambda _: None)

    class _Resp:
        status = 403
        reason = "Forbidden"

    calls = {"n": 0}

    class _Req:
        def execute(self):
            calls["n"] += 1
            raise HttpError(_Resp(), b'{"error":{"message":"insufficientPermissions"}}')

    with pytest.raises(HttpError):
        client_mod._with_backoff(_Req())
    assert calls["n"] == 1
