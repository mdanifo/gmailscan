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


def test_decode_keeps_the_outer_body_of_a_forwarded_message():
    """A forwarded message carries the forwarder's text/plain part and the
    original's inside a message/rfc822 part. The first one seen is the
    message; the inner one must not overwrite it."""
    payload = {
        "id": "m4",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Fwd: offer"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("outer")}},
                {
                    "mimeType": "message/rfc822",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("inner")}},
                        {"mimeType": "text/html", "body": {"data": _b64("<p>inner</p>")}},
                    ],
                },
            ],
        },
    }
    msg = decode_message(payload)
    assert msg.text == "outer"
    assert msg.html == "<p>inner</p>"  # the only HTML part, wherever it sits


def test_email_message_field_order_is_part_of_the_api():
    """Positional callers rely on the order. Reordering a field is a breaking
    change and belongs in the CHANGELOG, not in a refactor."""
    from dataclasses import fields

    assert [f.name for f in fields(EmailMessage)] == [
        "id",
        "subject",
        "sender",
        "date",
        "text",
        "html",
        "threadId",
        "to",
        "account",
        "received",
    ]
    positional = EmailMessage("1", "s", "f", "d", "plain", "<p>rich</p>", "t9")
    assert (positional.text, positional.html, positional.threadId) == ("plain", "<p>rich</p>", "t9")


# ------------------------------------------------- the body-preference split


def test_no_ambiguous_body_attribute():
    """Callers choose text_first or html_first; a `body` would hide the choice."""
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


def test_a_client_logs_which_gmailscan_it_is(caplog):
    """A consumer's own log names the release it runs."""
    import logging

    import gmailscan

    caplog.set_level(logging.INFO, logger="gmailscan.client")
    GmailClient("A@gmail.com", service=_FakeService([]))
    assert f"gmailscan {gmailscan.__version__} reading a@gmail.com" in caplog.text


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
    _write_token(tmp_path, "you@gmail.com")
    _write_token(tmp_path, "other@gmail.com")
    assert [c.account for c in clients()] == ["other@gmail.com", "you@gmail.com"]


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


def test_search_all_limit_applies_per_mailbox(monkeypatch, tmp_path):
    """Adding a mailbox must not eat into another's share of the limit."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    _write_token(tmp_path, "b@gmail.com")
    monkeypatch.setattr(
        "gmailscan.client.GmailClient.service",
        property(lambda self: _FakeService([_payload(msg_id=f"m{i}") for i in range(3)])),
    )
    accounts = [m.account for m in search_all("x", limit=2)]
    assert accounts.count("a@gmail.com") == 2
    assert accounts.count("b@gmail.com") == 2


def test_dump_path_is_unique_per_mailbox():
    """The same message id can exist in both mailboxes; one dump must not
    overwrite the other."""
    from pathlib import Path

    a = dump_path(Path("/d"), "amazon", "you@gmail.com", "m1")
    b = dump_path(Path("/d"), "amazon", "other@gmail.com", "m1")
    assert a != b
    assert "@" not in a.name  # safe on every filesystem


# ------------------------------------------------- one dead mailbox of several


def _dead(account):
    def _raise(self, *a, **k):
        raise GmailAuthRequired(f"refresh for {account} failed (invalid_grant)")

    return _raise


def test_a_dead_mailbox_does_not_blind_the_healthy_one(monkeypatch, tmp_path, caplog):
    """Tokens expire per account, so one mailbox can be dead while the other
    is fine. The sweep reads the live one and logs the dead one."""
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
    """A survey should not download years of bodies to read one header off
    each."""
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
    """The 403 that says rateLimitExceeded clears on its own."""
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
    """A revoked grant surfaces at once."""
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


def test_backoff_can_outlast_a_one_minute_quota_window(monkeypatch):
    """Gmail meters per minute, so the total wait has to exceed sixty seconds."""
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    class _Resp:
        status = 403
        reason = "Forbidden"

        def get(self, _k):
            return None

    class _Req:
        def execute(self):
            raise HttpError(_Resp(), b'{"error":{"message":"rateLimitExceeded"}}')

    with pytest.raises(HttpError):
        client_mod._with_backoff(_Req())

    assert sum(slept) > 60, f"gives up after only {sum(slept):.0f}s of waiting"


def test_backoff_stops_after_nine_attempts_with_each_wait_capped(monkeypatch):
    """The documented bounds: nine attempts, no wait over 90 s. The late waits
    are the capped 90 s with full jitter, so at least 45 s."""
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    class _Resp:
        status = 503
        reason = "Service Unavailable"

        def get(self, _k):
            return None

    calls = {"n": 0}

    class _Req:
        def execute(self):
            calls["n"] += 1
            raise HttpError(_Resp(), b"{}")

    with pytest.raises(HttpError):
        client_mod._with_backoff(_Req())

    assert calls["n"] == 9
    assert len(slept) == 8
    assert max(slept) <= 90.0
    assert slept[-1] >= 45.0


def test_a_retry_after_longer_than_two_minutes_is_clamped(monkeypatch):
    """A bad header must not park the run for an hour."""
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    class _Resp:
        status = 429
        reason = "Too Many Requests"

        def get(self, key):
            return "3600" if key == "retry-after" else None

    calls = {"n": 0}

    class _Req:
        def execute(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HttpError(_Resp(), b"{}")
            return {"ok": True}

    assert client_mod._with_backoff(_Req()) == {"ok": True}
    assert slept == [120.0]


@pytest.mark.parametrize(
    ("status", "body", "retried"),
    [
        (429, b"{}", True),
        (500, b"{}", True),
        (502, b"{}", True),
        (503, b"{}", True),
        (504, b"{}", True),
        (403, b'{"error":{"message":"quotaExceeded"}}', True),
        (403, b'{"error":{"message":"insufficientPermissions"}}', False),
        (400, b'{"error":{"message":"rateLimitExceeded"}}', False),
        (404, b"{}", False),
        (401, b"{}", False),
    ],
)
def test_only_rate_limits_and_server_errors_are_retried(monkeypatch, status, body, retried):
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    monkeypatch.setattr("time.sleep", lambda _: None)

    class _Resp:
        reason = "x"

        def get(self, _k):
            return None

    _Resp.status = status
    calls = {"n": 0}

    class _Req:
        def execute(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HttpError(_Resp(), body)
            return {"ok": True}

    if retried:
        assert client_mod._with_backoff(_Req()) == {"ok": True}
        assert calls["n"] == 2
    else:
        with pytest.raises(HttpError):
            client_mod._with_backoff(_Req())
        assert calls["n"] == 1


def test_backoff_obeys_retry_after_when_google_sends_one(monkeypatch):
    """Google's Retry-After wins over the computed delay."""
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    class _Resp:
        status = 429
        reason = "Too Many Requests"

        def get(self, key):
            return "7" if key == "retry-after" else None

    calls = {"n": 0}

    class _Req:
        def execute(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HttpError(_Resp(), b"{}")
            return {"ok": True}

    assert client_mod._with_backoff(_Req()) == {"ok": True}
    assert slept == [7.0]


# ------------------------------------- every request rides out the quota


class _RateLimitedOnce:
    """A request Gmail refuses once for quota, then serves."""

    def __init__(self, value):
        self._value = value
        self.calls = 0

    def execute(self):
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 429
            reason = "Too Many Requests"

        self.calls += 1
        if self.calls == 1:
            raise HttpError(_Resp(), b'{"error":{"message":"rateLimitExceeded"}}')
        return self._value


def test_get_thread_rides_out_a_rate_limit(monkeypatch):
    """threads.get goes through the same retry as messages.list."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    request = _RateLimitedOnce({"messages": [_payload("m1"), _payload("m2")]})

    class _Threads:
        def get(self, *, userId, id, format):  # noqa: A002 - mirrors the Google signature
            return request

    service = _FakeService([])
    service.threads = lambda: _Threads()
    thread = GmailClient("a@gmail.com", service=service).get_thread("t1")

    assert [m.id for m in thread] == ["m1", "m2"]
    assert request.calls == 2


def test_raw_rides_out_a_rate_limit(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    request = _RateLimitedOnce({"raw": _b64("From: a@b\r\n\r\nbody")})

    class _Messages(_FakeMessages):
        def get(self, **_kw):
            return request

    service = _FakeService([])
    service._messages = _Messages([])

    assert b"From: a@b" in GmailClient("a@gmail.com", service=service).raw("m1")
    assert request.calls == 2


def test_no_request_bypasses_the_backoff():
    """_with_backoff is the only place a Gmail request may be executed, so a
    new method cannot skip the retry by accident."""
    import inspect

    from gmailscan import client as client_mod

    source = inspect.getsource(client_mod)
    backoff = inspect.getsource(client_mod._with_backoff)
    assert source.count(".execute()") == backoff.count(".execute()") == 1


# ------------------------------------------------ windowing and list sizing


def test_search_applies_the_before_filter():
    captured = {}

    class _Recording(_FakeMessages):
        def list(self, **kw):
            captured.update(kw)
            return super().list(**kw)

    service = _FakeService([_payload()])
    service._messages = _Recording([_payload()])
    list(GmailClient("a@gmail.com", service=service).search("x", before=date(2026, 9, 1)))
    assert "before:2026/09/01" in captured["q"]


def test_search_never_lists_more_ids_than_it_will_fetch():
    """A list call costs 5 units whatever it returns, but ids past the limit
    are paid for and dropped; ask for what is left."""
    sizes = []

    class _Recording(_FakeMessages):
        def list(self, **kw):
            sizes.append(kw["maxResults"])
            return super().list(**kw)

    service = _FakeService([_payload(msg_id=f"m{i}") for i in range(5)])
    service._messages = _Recording(service._messages._payloads.values())
    list(GmailClient("a@gmail.com", service=service).search("x", limit=3))
    assert sizes == [3]

    sizes.clear()
    list(GmailClient("a@gmail.com", service=service).search("x", limit=2000))
    assert sizes == [500]  # Gmail's own ceiling for one list call


def test_received_comes_from_gmails_own_timestamp():
    """The Date header is whatever the sender wrote, in its own zone;
    internalDate is Gmail's receipt time and the one to sort or window by."""
    from datetime import datetime, timezone

    payload = _payload()
    payload["internalDate"] = "1756044131000"  # 2025-08-24T14:02:11Z
    msg = decode_message(payload)
    assert msg.received == datetime(2025, 8, 24, 14, 2, 11, tzinfo=timezone.utc)
    assert msg.date == "Mon, 24 Aug 2026 10:02:11 -0400"  # the header is untouched


def test_received_is_none_on_a_hand_built_fixture():
    assert decode_message(_payload()).received is None
    assert EmailMessage(id="1", subject="s", sender="f", date="d").received is None


# ------------------------------------------------ the rest of the retry loop


def test_a_retry_after_that_is_not_a_number_falls_back_to_the_guess(monkeypatch):
    """Believe the server, but only when it says something usable."""
    from googleapiclient.errors import HttpError

    from gmailscan import client as client_mod

    slept = []
    monkeypatch.setattr("time.sleep", slept.append)

    class _Resp:
        status = 429
        reason = "Too Many Requests"

        def get(self, key):
            return "soon" if key == "retry-after" else None

    calls = {"n": 0}

    class _Req:
        def execute(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HttpError(_Resp(), b"{}")
            return {"ok": True}

    assert client_mod._with_backoff(_Req()) == {"ok": True}
    assert len(slept) == 1 and 0.5 <= slept[0] <= 1.0  # attempt 0: 2**0 with full jitter


def test_an_empty_thread_id_reads_nothing_rather_than_asking_gmail():
    class _Explodes:
        def users(self):
            raise AssertionError("no request should be made")

    assert GmailClient("a@gmail.com", service=_Explodes()).get_thread("") == []


# ------------------------------------------- listing without reading, skipping


class _Paged(_FakeMessages):
    """messages.list with real pagination, recording what each call asked for."""

    def __init__(self, payloads):
        super().__init__(payloads)
        self.list_calls: list[dict] = []
        self.get_calls: list[str] = []

    def list(self, **kw):
        self.list_calls.append(kw)
        ids = sorted(self._payloads)
        start = int(kw.get("pageToken") or 0)
        size = kw["maxResults"]
        page = ids[start : start + size]
        body = {"messages": [{"id": i, "threadId": self._payloads[i]["threadId"]} for i in page]}
        if start + size < len(ids):
            body["nextPageToken"] = str(start + size)
        return _FakeExec(body)

    def get(self, *, userId, id, format, **kw):  # noqa: A002 - mirrors the Google signature
        self.get_calls.append(id)
        return super().get(userId=userId, id=id, format=format)


def _paged_client(n):
    payloads = [_payload(msg_id=f"m{i:03d}", thread=f"t{i % 3}") for i in range(n)]
    service = _FakeService([])
    service._messages = _Paged(payloads)
    return GmailClient("a@gmail.com", service=service), service._messages


def test_list_ids_reads_nothing_and_carries_the_thread():
    client, messages = _paged_client(5)
    refs = client.list_ids("x", after=date(2026, 9, 1))
    assert [r.id for r in refs] == ["m000", "m001", "m002", "m003", "m004"]
    assert refs[4].threadId == "t1"
    assert messages.get_calls == []  # nothing read
    assert messages.list_calls[0]["q"] == "x after:2026/09/01"


def test_list_ids_pages_at_500_and_stops_at_the_limit():
    client, messages = _paged_client(1200)
    assert len(client.list_ids("x", limit=1200)) == 1200
    assert [c["maxResults"] for c in messages.list_calls] == [500, 500, 200]

    messages.list_calls.clear()
    assert len(client.list_ids("x", limit=3)) == 3
    assert [c["maxResults"] for c in messages.list_calls] == [3]


def test_get_message_reads_one_through_the_backoff(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    request = _RateLimitedOnce(_payload("m9", thread="t9"))

    class _Messages(_FakeMessages):
        def get(self, **_kw):
            return request

    service = _FakeService([])
    service._messages = _Messages([])
    msg = GmailClient("a@gmail.com", service=service).get_message("m9")
    assert (msg.id, msg.threadId, msg.account) == ("m9", "t9", "a@gmail.com")
    assert request.calls == 2


def test_skip_ids_are_listed_but_never_read():
    """A sweep that re-reads a window every run pays for new mail only."""
    client, messages = _paged_client(5)
    found = list(client.search("x", skip_ids={"m000", "m002", "m004"}))
    assert [m.id for m in found] == ["m001", "m003"]
    assert messages.get_calls == ["m001", "m003"]


def test_skipped_ids_do_not_count_toward_the_limit():
    client, messages = _paged_client(10)
    known = {f"m{i:03d}" for i in range(8)}
    found = list(client.search("x", limit=2, skip_ids=known))
    assert [m.id for m in found] == ["m008", "m009"]
    assert [c["maxResults"] for c in messages.list_calls] == [500]  # full pages when skipping


def test_skip_ids_takes_any_container():
    """A database-backed lookup works as well as a set."""

    class _AskTheDatabase:
        def __init__(self):
            self.asked: list[str] = []

        def __contains__(self, message_id):
            self.asked.append(message_id)
            return message_id.endswith("1")

    client, _messages = _paged_client(3)
    lookup = _AskTheDatabase()
    assert [m.id for m in client.search("x", skip_ids=lookup)] == ["m000", "m002"]
    assert lookup.asked == ["m000", "m001", "m002"]


def test_search_all_passes_skip_ids_to_every_mailbox(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    seen = {}

    def search(self, query, **kw):
        seen[self.account] = kw.get("skip_ids")
        return iter(())

    monkeypatch.setattr(GmailClient, "search", search)
    list(search_all("x", skip_ids={"m1"}))
    assert seen == {"a@gmail.com": {"m1"}}
