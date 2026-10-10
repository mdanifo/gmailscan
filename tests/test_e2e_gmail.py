"""End to end: the real stack against a fake Google.

Nothing in gmailscan is faked here. A token file on disk goes through
load_credentials, google-auth refreshes it over HTTP, the discovery client
builds the Gmail service from Google's own discovery document, httplib2 makes
the calls, and HttpError comes back the way the real API would send it. Only
the server on the other end is ours (see conftest.FakeGoogle).

This is the tier that would catch a change in google-auth or the discovery
client that the unit tests' hand-rolled fakes cannot see.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

import pytest

from conftest import message_payload, write_authorized_user
from gmailscan import GmailClient, cli, health, health_detail, search_all

pytestmark = pytest.mark.e2e


@pytest.fixture
def mailbox(real_client_stack, tmp_path, monkeypatch):
    """One authorized account with three messages in two threads."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    real_client_stack.add(
        message_payload("m1", thread="t1", subject="Interview", sender="recruiter@corp.example"),
        message_payload("m2", thread="t1", subject="Re: Interview", sender="me@example.com"),
        message_payload("m3", thread="t2", subject="Your order", internal_date="1756130531000"),
    )
    write_authorized_user(tmp_path, "a@gmail.com", real_client_stack, refresh_token="rt-a")
    return real_client_stack


def test_a_search_refreshes_the_grant_then_reads_gmail(mailbox, tmp_path):
    found = list(GmailClient("a@gmail.com").search("subject:interview"))

    # The stale access token was refreshed against the token endpoint once,
    # written back to disk, and used as the bearer token on every Gmail call.
    assert mailbox.tokens_issued == 1
    assert json.loads((tmp_path / "token-a@gmail.com.json").read_text())["token"] == "access-1"
    gmail = mailbox.gmail_requests()
    assert gmail and all(r["authorization"] == "Bearer access-1" for r in gmail)

    # One list, then one get per message, in the format a parser needs.
    assert [r["path"].rsplit("/", 1)[-1] for r in gmail] == ["messages", "m1", "m2", "m3"]
    assert gmail[0]["query"]["q"] == "subject:interview"
    assert all(r["query"]["format"] == "full" for r in gmail[1:])

    assert [m.subject for m in found] == ["Interview", "Re: Interview", "Your order"]
    assert found[0].text == "hello" and found[0].html == "<p>hello</p>"
    assert found[0].account == "a@gmail.com"
    assert found[2].received == datetime(2025, 8, 25, 14, 2, 11, tzinfo=timezone.utc)


def test_headers_only_and_the_window_reach_the_wire(mailbox):
    list(
        GmailClient("a@gmail.com").search(
            "from:shop", after=date(2026, 8, 1), before=date(2026, 9, 1), limit=2, headers_only=True
        )
    )
    gmail = mailbox.gmail_requests()
    assert gmail[0]["query"]["q"] == "from:shop after:2026/08/01 before:2026/09/01"
    assert gmail[0]["query"]["maxResults"] == "2"
    assert len(gmail) == 3  # the list, then only two gets: limit is a hard stop
    assert gmail[1]["query"]["format"] == "metadata"
    assert set(gmail[1]["query"]["metadataHeaders"].split(",")) == {"From", "To", "Subject", "Date"}


def test_a_quota_refusal_is_waited_out_not_raised(mailbox, monkeypatch):
    """A real HttpError from the real client, carrying Google's Retry-After, on
    the first request of the sweep. The sweep must come back whole, having
    waited exactly what Google asked."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    mailbox.refuse_next.append((429, {"Retry-After": "3"}))

    assert len(list(GmailClient("a@gmail.com").search("x"))) == 3
    assert slept == [3.0]
    # The refused list was repeated, then the three reads went through.
    calls = [r["path"].rsplit("/", 1)[-1] for r in mailbox.gmail_requests()]
    assert calls == ["messages", "messages", "m1", "m2", "m3"]


def test_get_thread_and_raw_go_through_the_same_retry(mailbox, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    client = GmailClient("a@gmail.com")

    mailbox.refuse_next.append((403, {}))
    thread = client.get_thread("t1")
    assert [m.id for m in thread] == ["m1", "m2"]

    mailbox.refuse_next.append((429, {}))
    assert b"Subject: Interview" in client.raw("m1")


def test_a_revoked_grant_is_reported_as_such_and_does_not_blind_the_other_mailbox(
    mailbox, tmp_path, caplog
):
    """Google answers the refresh with invalid_grant; google-auth turns that
    into a RefreshError; health() calls it revoked and search_all skips the
    mailbox, reading the healthy one. No fake stands in for any of that."""
    write_authorized_user(tmp_path, "dead@gmail.com", mailbox, refresh_token="rt-dead")
    mailbox.revoked.add("rt-dead")

    assert health("a@gmail.com") == "ok"
    state, detail = health_detail("dead@gmail.com")
    assert state == "revoked"
    assert "invalid_grant" in detail

    with caplog.at_level(logging.WARNING):
        found = list(search_all("x"))
    assert {m.account for m in found} == {"a@gmail.com"}
    assert "skipping dead@gmail.com" in caplog.text


def test_status_sees_the_same_grants_a_sweep_would(mailbox, tmp_path, capsys):
    write_authorized_user(tmp_path, "dead@gmail.com", mailbox, refresh_token="rt-dead")
    mailbox.revoked.add("rt-dead")

    assert cli.main(["--status"]) == 1
    out = capsys.readouterr().out
    assert "a@gmail.com" in out and " OK " in out
    assert "dead@gmail.com" in out and "REVOKED" in out and "invalid_grant" in out

    assert cli.main(["--status", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["health"]["a@gmail.com"]["state"] == "ok"
    assert report["health"]["dead@gmail.com"]["state"] == "revoked"
