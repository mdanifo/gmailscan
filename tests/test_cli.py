"""The consent and push commands, with the OAuth flow and AWS faked.

--status is covered in test_auth.py next to the grant-age helpers it prints.
What is here is everything else gmailscan-auth does: deciding whether a consent
is needed, running one headless or with a local listener, writing the token and
its .granted sidecar, and copying tokens to Secrets Manager.
"""

from __future__ import annotations

import json
import sys

import pytest

from gmailscan import GmailAuthRequired, cli


@pytest.fixture(autouse=True)
def _token_dir(monkeypatch, tmp_path):
    for key in ("GMAILSCAN_TOKEN", "GMAILSCAN_ACCOUNTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    return tmp_path


class _FakeCredentials:
    def __init__(self, refresh_token="refresh"):
        self.refresh_token = refresh_token

    def to_json(self):
        return json.dumps(
            {"token": "access", "refresh_token": self.refresh_token, "client_id": "c"}
        )


class _FakeFlow:
    """InstalledAppFlow with both consent paths, recording what was asked."""

    def __init__(self, creds=None):
        self.credentials = creds or _FakeCredentials()
        self.calls: list[tuple] = []
        self.redirect_uri = None

    def run_local_server(self, **kw):
        self.calls.append(("local", kw))
        return self.credentials

    def authorization_url(self, **kw):
        self.calls.append(("url", kw))
        return "https://accounts.google.com/o/oauth2/auth?client_id=c", "state"

    def fetch_token(self, **kw):
        self.calls.append(("fetch", kw))


def _install_flow(monkeypatch, flow):
    from google_auth_oauthlib.flow import InstalledAppFlow

    monkeypatch.setattr(
        InstalledAppFlow, "from_client_secrets_file", classmethod(lambda cls, *a, **k: flow)
    )


# ---------------------------------------------------------------- consent


def test_consent_writes_the_token_and_records_when_it_was_granted(monkeypatch, tmp_path, capsys):
    flow = _FakeFlow()
    _install_flow(monkeypatch, flow)
    client_json = tmp_path / "client.json"
    client_json.write_text("{}")

    assert cli.main(["--account", "New@Gmail.com", "--client-secret-file", str(client_json)]) == 0

    token = tmp_path / "token-new@gmail.com.json"  # lower-cased: one mailbox, one file
    assert json.loads(token.read_text())["refresh_token"] == "refresh"
    assert cli._granted_marker(token).exists()
    assert "0d ago" in cli._granted_age(token)
    assert flow.calls == [("local", {"port": 8765, "prompt": "consent", "access_type": "offline"})]
    out = capsys.readouterr().out
    assert "Authorized new@gmail.com" in out
    assert "gmailscan-auth --push --account new@gmail.com" in out  # the step that gets skipped


def test_manual_consent_takes_the_pasted_redirect_back_into_the_same_flow(
    monkeypatch, tmp_path, capsys
):
    """The PKCE verifier lives on the flow object; the pasted URL must finish
    the flow that printed it, not a new one."""
    flow = _FakeFlow()
    _install_flow(monkeypatch, flow)
    (tmp_path / "client.json").write_text("{}")
    monkeypatch.setattr(
        "builtins.input", lambda _prompt: "  http://localhost:9999/?code=abc&state=state  "
    )

    assert (
        cli.main(
            [
                "--account",
                "a@gmail.com",
                "--client-secret-file",
                str(tmp_path / "client.json"),
                "--manual",
                "--port",
                "9999",
            ]
        )
        == 0
    )
    assert flow.redirect_uri == "http://localhost:9999/"
    assert (
        "fetch",
        {"authorization_response": "http://localhost:9999/?code=abc&state=state"},
    ) in flow.calls
    assert "Open this in a browser" in capsys.readouterr().out


def test_a_grant_without_a_refresh_token_is_refused_not_saved(monkeypatch, tmp_path, capsys):
    """An access token alone dies in an hour, inside an overnight timer."""
    _install_flow(monkeypatch, _FakeFlow(_FakeCredentials(refresh_token=None)))
    (tmp_path / "client.json").write_text("{}")

    assert (
        cli.main(
            ["--account", "a@gmail.com", "--client-secret-file", str(tmp_path / "client.json")]
        )
        == 1
    )
    assert not (tmp_path / "token-a@gmail.com.json").exists()
    assert "no refresh token" in capsys.readouterr().err


def test_force_re_consents_an_account_that_already_has_a_token(monkeypatch, tmp_path):
    (tmp_path / "token-a@gmail.com.json").write_text(
        json.dumps({"refresh_token": "old", "client_id": "c"})
    )
    flow = _FakeFlow(_FakeCredentials(refresh_token="new"))
    _install_flow(monkeypatch, flow)
    (tmp_path / "client.json").write_text("{}")

    assert (
        cli.main(
            [
                "--account",
                "a@gmail.com",
                "--client-secret-file",
                str(tmp_path / "client.json"),
                "--force",
            ]
        )
        == 0
    )
    assert json.loads((tmp_path / "token-a@gmail.com.json").read_text())["refresh_token"] == "new"


def test_consent_without_the_auth_extra_names_the_install(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", None)  # import raises
    (tmp_path / "client.json").write_text("{}")
    assert (
        cli.main(
            ["--account", "a@gmail.com", "--client-secret-file", str(tmp_path / "client.json")]
        )
        == 2
    )
    assert 'pip install "gmailscan[auth]"' in capsys.readouterr().err


def test_the_granted_marker_is_best_effort(monkeypatch, tmp_path):
    """A read-only token store must not fail a consent that worked."""
    token = tmp_path / "token-a@gmail.com.json"
    token.write_text("{}")

    def denied(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", denied)
    cli._record_grant(token)  # must not raise
    assert "unknown" in cli._granted_age(token)


# ------------------------------------------------------------------- push


def test_push_copies_the_named_account_and_stamps_when(monkeypatch, capsys):
    seen = {}

    def push_tokens(accounts, *, secret_name, region, granted_at):
        seen.update(
            accounts=accounts, secret_name=secret_name, region=region, granted_at=granted_at
        )
        return ["a@gmail.com"]

    monkeypatch.setattr("gmailscan.secrets.push_tokens", push_tokens)
    assert (
        cli.main(
            ["--push", "--account", "a@gmail.com", "--secret-name", "s", "--region", "us-east-1"]
        )
        == 0
    )
    assert (
        seen["accounts"] == ["a@gmail.com"]
        and seen["secret_name"] == "s"
        and seen["region"] == "us-east-1"
    )
    assert seen["granted_at"].startswith("20")
    assert "Pushed to 's': a@gmail.com" in capsys.readouterr().out


def test_push_without_an_account_pushes_every_local_token(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "gmailscan.secrets.push_tokens",
        lambda accounts, **kw: (
            seen.setdefault("accounts", accounts) or ["a@gmail.com", "b@gmail.com"]
        ),
    )
    assert cli.main(["--push"]) == 0
    assert seen["accounts"] is None


def test_push_reports_what_the_store_refused(monkeypatch, capsys):
    def refused(*_a, **_k):
        raise GmailAuthRequired("No local Gmail tokens to push.")

    monkeypatch.setattr("gmailscan.secrets.push_tokens", refused)
    assert cli.main(["--push"]) == 1
    assert "No local Gmail tokens to push" in capsys.readouterr().err


def test_push_without_boto3_says_which_extra(monkeypatch, capsys):
    """The error a real missing boto3 produces, surfaced through the CLI."""
    monkeypatch.setitem(sys.modules, "boto3", None)
    (cli.token_path("a@gmail.com")).write_text(json.dumps({"refresh_token": "r", "client_id": "c"}))
    assert cli.main(["--push", "--account", "a@gmail.com"]) == 1
    assert 'pip install "gmailscan[secrets]"' in capsys.readouterr().err


# ------------------------------------------------------------------ routing


def test_main_routes_push_before_status_and_status_before_consent(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_push", lambda args: calls.append("push") or 0)
    monkeypatch.setattr(cli, "_status", lambda: calls.append("status") or 0)
    monkeypatch.setattr(cli, "_status_json", lambda: calls.append("json") or 0)
    monkeypatch.setattr(
        cli, "_authorize", lambda account, args: calls.append(("auth", account)) or 0
    )

    cli.main(["--push", "--account", "a@gmail.com"])
    cli.main(["--status", "--account", "a@gmail.com"])
    cli.main([])
    cli.main(["--json"])
    cli.main(["--account", " A@Gmail.com "])
    assert calls == ["push", "status", "status", "json", ("auth", "a@gmail.com")]


def test_status_with_nothing_authorized_says_so_and_how_to_fix_it(capsys):
    assert cli.main(["--status"]) == 1
    out = capsys.readouterr().out
    assert "No Gmail account is authorized here" in out
    assert "gmailscan-auth --account" in out


def test_status_json_with_nothing_authorized_is_still_json(capsys):
    assert cli.main(["--status", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["accounts"] == [] and report["health"] == {}


# ------------------------------------------ deciding whether to consent at all


def test_an_existing_token_is_left_alone_without_force(tmp_path, capsys):
    (tmp_path / "token-a@gmail.com.json").write_text("{}")
    assert cli.main(["--account", "a@gmail.com"]) == 0
    out = capsys.readouterr().out
    assert "already has a token" in out and "--force" in out


def test_a_first_consent_needs_the_client_json_and_says_where_it_comes_from(capsys):
    assert cli.main(["--account", "a@gmail.com"]) == 2
    err = capsys.readouterr().err
    assert "--client-secret-file" in err
    assert "PUBLISH THE APP TO PRODUCTION" in err  # the 7-day trap, named up front
