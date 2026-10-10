"""Shared fixtures. The important one is a fake Google.

The unit tests fake the Gmail API one object at a time. The end-to-end tier
instead runs the real stack -- token file, google-auth refresh over HTTP, the
discovery client, httplib2 -- against a local server that speaks just enough of
oauth2.googleapis.com and gmail.googleapis.com for what this package does. It
records every request, so a test can assert what was actually sent.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

PAST = "2020-01-01T00:00:00Z"


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message_payload(
    msg_id: str,
    *,
    thread: str = "t1",
    subject: str = "Your order",
    sender: str = "shop@example.com",
    to: str = "me@example.com",
    date: str = "Mon, 24 Aug 2026 10:02:11 -0400",
    text: str | None = "hello",
    html: str | None = "<p>hello</p>",
    internal_date: str = "1756044131000",
) -> dict[str, Any]:
    """A ``users.messages.get(format=full)`` document, as Gmail would return it."""
    parts = []
    if text is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64url(text)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64url(html)}})
    return {
        "id": msg_id,
        "threadId": thread,
        "internalDate": internal_date,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "To", "value": to},
                {"name": "Date", "value": date},
            ],
            "parts": parts,
        },
    }


class FakeGoogle:
    """The token endpoint and the four Gmail calls this package makes.

    ``revoked`` holds refresh tokens the token endpoint refuses with
    ``invalid_grant``, the way Google reports both revocation and a Testing-mode
    grant past seven days. ``refuse_next`` queues (status, headers) responses
    that the next Gmail API requests get instead of a real answer, which is how
    a test puts a quota refusal in the path of the real retry code.
    """

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.revoked: set[str] = set()
        self.refuse_next: list[tuple[int, dict[str, str]]] = []
        self.requests: list[dict[str, Any]] = []
        self.tokens_issued = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # -- what a test configures ------------------------------------------------

    def add(self, *payloads: dict[str, Any]) -> None:
        for payload in payloads:
            self.messages[payload["id"]] = payload

    @property
    def token_uri(self) -> str:
        return f"{self.base}/token"

    def gmail_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"].startswith("/gmail/")]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- the server --------------------------------------------------------------

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # keep pytest output clean
                pass

            def _send(
                self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None
            ) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _record(self, body: bytes = b"") -> dict[str, Any]:
                url = urlparse(self.path)
                record = {
                    "method": self.command,
                    "path": url.path,
                    # A repeated parameter (metadataHeaders=From&metadataHeaders=To)
                    # is kept whole, comma-joined, the way a test wants to read it.
                    "query": {k: ",".join(v) for k, v in parse_qs(url.query).items()},
                    "authorization": self.headers.get("Authorization", ""),
                    "body": body.decode(),
                }
                fake.requests.append(record)
                return record

            def do_POST(self) -> None:  # noqa: N802 - http.server's naming
                length = int(self.headers.get("Content-Length") or 0)
                record = self._record(self.rfile.read(length))
                if record["path"] != "/token":
                    self._send(404, {"error": "not found"})
                    return
                form = {k: v[0] for k, v in parse_qs(record["body"]).items()}
                if form.get("refresh_token") in fake.revoked:
                    self._send(
                        400,
                        {
                            "error": "invalid_grant",
                            "error_description": "Token has been expired or revoked.",
                        },
                    )
                    return
                fake.tokens_issued += 1
                self._send(
                    200,
                    {
                        "access_token": f"access-{fake.tokens_issued}",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                        "scope": "https://www.googleapis.com/auth/gmail.readonly",
                    },
                )

            def do_GET(self) -> None:  # noqa: N802
                record = self._record()
                path, query = record["path"], record["query"]
                if fake.refuse_next and path.startswith("/gmail/"):
                    status, headers = fake.refuse_next.pop(0)
                    reason = "rateLimitExceeded"
                    self._send(
                        status,
                        {
                            "error": {
                                "code": status,
                                "message": "User-rate limit exceeded.",
                                "errors": [
                                    {"reason": reason, "message": "User-rate limit exceeded."}
                                ],
                            }
                        },
                        headers,
                    )
                    return
                if path == "/gmail/v1/users/me/messages":
                    ids = sorted(fake.messages)
                    start = int(query.get("pageToken") or 0)
                    size = int(query.get("maxResults") or 100)
                    page = ids[start : start + size]
                    body: dict[str, Any] = {
                        "messages": [
                            {"id": i, "threadId": fake.messages[i]["threadId"]} for i in page
                        ],
                        "resultSizeEstimate": len(ids),
                    }
                    if start + size < len(ids):
                        body["nextPageToken"] = str(start + size)
                    self._send(200, body)
                    return
                if path.startswith("/gmail/v1/users/me/messages/"):
                    msg_id = path.rsplit("/", 1)[1]
                    payload = fake.messages.get(msg_id)
                    if payload is None:
                        self._send(404, {"error": {"code": 404, "message": "Not Found"}})
                        return
                    self._send(200, _in_format(payload, query))
                    return
                if path.startswith("/gmail/v1/users/me/threads/"):
                    thread_id = path.rsplit("/", 1)[1]
                    members = [p for p in fake.messages.values() if p["threadId"] == thread_id]
                    if not members:
                        self._send(404, {"error": {"code": 404, "message": "Not Found"}})
                        return
                    self._send(200, {"id": thread_id, "messages": members})
                    return
                self._send(404, {"error": {"code": 404, "message": f"no route for {path}"}})

        return Handler


def _in_format(payload: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
    """Shape a stored full message the way Gmail does for each ``format``."""
    fmt = query.get("format", "full")
    if fmt == "raw":
        sender, subject = _header(payload, "From"), _header(payload, "Subject")
        text = f"From: {sender}\r\nSubject: {subject}\r\n\r\nbody"
        return {"id": payload["id"], "threadId": payload["threadId"], "raw": _b64url(text)}
    if fmt == "metadata":
        wanted = {h.lower() for h in query.get("metadataHeaders", "").split(",") if h}
        headers = [
            h for h in payload["payload"]["headers"] if not wanted or h["name"].lower() in wanted
        ]
        return {
            **{k: v for k, v in payload.items() if k != "payload"},
            "payload": {"mimeType": payload["payload"]["mimeType"], "headers": headers},
        }
    return payload


def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload["payload"]["headers"]:
        if h["name"] == name:
            return str(h["value"])
    return ""


def gmail_discovery_document() -> dict[str, Any]:
    """The Gmail v1 discovery document google-api-python-client ships."""
    import googleapiclient

    path = os.path.join(
        os.path.dirname(googleapiclient.__file__), "discovery_cache", "documents", "gmail.v1.json"
    )
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def fake_google():
    fake = FakeGoogle()
    yield fake
    fake.close()


@pytest.fixture
def real_client_stack(fake_google, monkeypatch):
    """Point the real discovery client at the fake Google.

    GmailClient.service calls googleapiclient.discovery.build; this swaps in a
    build from the shipped Gmail discovery document with its root URL rewritten,
    so every other layer -- credentials, httplib2, error handling -- is real.
    """
    from google.oauth2 import credentials as oauth2_credentials
    from googleapiclient import discovery

    # google-auth ignores the token_uri in an authorized-user file and always
    # refreshes against Google ("so account can be refreshed", its comment
    # says). Without this the first version of these tests sent a made-up
    # refresh token to oauth2.googleapis.com, which answered invalid_client.
    assert hasattr(oauth2_credentials, "_GOOGLE_OAUTH2_TOKEN_ENDPOINT"), (
        "google-auth renamed the token endpoint constant; find where "
        "from_authorized_user_info gets its token_uri and patch that"
    )
    monkeypatch.setattr(oauth2_credentials, "_GOOGLE_OAUTH2_TOKEN_ENDPOINT", fake_google.token_uri)

    def build(*_args: Any, credentials: Any = None, **_kwargs: Any) -> Any:
        doc = gmail_discovery_document()
        doc["rootUrl"] = fake_google.base + "/"
        doc.pop("mtlsRootUrl", None)
        return discovery.build_from_document(doc, credentials=credentials)

    monkeypatch.setattr(discovery, "build", build)
    return fake_google


def write_authorized_user(
    directory, account: str, fake: FakeGoogle, *, refresh_token: str, expiry: str = PAST
):
    """A token file exactly as gmailscan-auth would leave it, pointed at the fake."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"token-{account}.json"
    path.write_text(
        json.dumps(
            {
                "token": "stale",
                "refresh_token": refresh_token,
                "client_id": "client-id",
                "client_secret": "client-secret",
                "token_uri": fake.token_uri,
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "expiry": expiry,
            }
        )
    )
    return path
