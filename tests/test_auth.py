"""Token discovery: the resolution order, account discovery, and the scope pin."""

from __future__ import annotations

import json

import pytest

from gmailscan import auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("GMAILSCAN_TOKEN", "GMAILSCAN_TOKEN_DIR", "GMAILSCAN_ACCOUNTS"):
        monkeypatch.delenv(key, raising=False)
    yield


def _write_token(directory, account, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"token-{account}.json"
    path.write_text(json.dumps({"refresh_token": "r", "client_id": "c", **extra}))
    return path


def test_scope_is_readonly_and_nothing_else():
    """The whole safety story rests on this. Widening it should fail loudly."""
    assert auth.SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_default_dir_is_the_canonical_shared_location():
    assert auth.DEFAULT_TOKEN_DIR.name == "google-oauth"
    assert auth.DEFAULT_TOKEN_DIR.parent.name == ".config"


def test_explicit_token_file_wins(monkeypatch, tmp_path):
    explicit = tmp_path / "somewhere-else.json"
    monkeypatch.setenv("GMAILSCAN_TOKEN", str(explicit))
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path / "ignored"))
    assert auth.token_path("a@gmail.com") == explicit


def test_token_dir_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    assert auth.token_path("A@Gmail.com") == tmp_path / "token-a@gmail.com.json"


def test_account_is_lowercased_so_one_mailbox_is_one_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    assert auth.token_path("You@Gmail.com") == auth.token_path("you@gmail.com")


def test_authorized_accounts_discovers_every_mailbox(monkeypatch, tmp_path):
    """With GMAILSCAN_ACCOUNTS unset, every mailbox with a token is read."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "you@gmail.com")
    _write_token(tmp_path, "other@gmail.com")
    assert auth.authorized_accounts() == ["other@gmail.com", "you@gmail.com"]


def test_authorized_accounts_ignores_unrelated_files(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    (tmp_path / "credentials.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("hi")
    assert auth.authorized_accounts() == ["a@gmail.com"]


def test_accounts_env_pins_the_list(monkeypatch, tmp_path):
    """Per-project choice: a project that wants one mailbox says so."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    _write_token(tmp_path, "b@gmail.com")
    monkeypatch.setenv("GMAILSCAN_ACCOUNTS", "b@gmail.com")
    assert auth.authorized_accounts() == ["b@gmail.com"]


def test_missing_token_dir_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path / "nope"))
    assert auth.authorized_accounts() == []
    assert auth.is_configured() is False


def test_is_configured_never_imports_google(monkeypatch, tmp_path):
    """A UI calls this on a box that has never seen a token."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    assert auth.is_configured("a@gmail.com") is False
    _write_token(tmp_path, "a@gmail.com")
    assert auth.is_configured("a@gmail.com") is True


def test_missing_token_names_the_path_and_the_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    with pytest.raises(auth.GmailAuthRequired) as excinfo:
        auth.load_credentials("nobody@gmail.com")
    message = str(excinfo.value)
    assert "token-nobody@gmail.com.json" in message
    assert "gmailscan-auth" in message


def test_setup_hint_explains_the_seven_day_expiry():
    """The recurring failure across these projects. The hint must name the cause."""
    assert "7 days" in auth.SETUP_HINT
    assert "Testing" in auth.SETUP_HINT


def _authorized_user(directory, account, *, expiry, **overrides):
    """A token file google-auth will parse, unlike the stubs above."""
    doc = {
        "token": "access",
        "refresh_token": "refresh",
        "client_id": "c",
        "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token",
        "expiry": expiry,
        **overrides,
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"token-{account}.json"
    path.write_text(json.dumps({k: v for k, v in doc.items() if v is not None}))
    return path


FUTURE = "2099-01-01T00:00:00Z"
PAST = "2020-01-01T00:00:00Z"


def test_health_reports_a_missing_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    assert auth.health("nobody@gmail.com") == "missing"


def test_health_reports_a_live_token_ok_without_a_refresh(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=FUTURE)

    def no_network(*_a, **_k):
        raise AssertionError("a valid access token needs no refresh")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", no_network)
    assert auth.health("a@gmail.com") == "ok"


def test_a_revoked_grant_is_revoked_while_is_configured_still_says_yes(monkeypatch, tmp_path):
    """The file outlives the grant, so a file check keeps saying yes."""
    from google.auth.exceptions import RefreshError

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=PAST)

    def refused(*_a, **_k):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refused)
    assert auth.health("a@gmail.com") == "revoked"
    assert auth.is_configured("a@gmail.com") is True  # still a file check


def test_an_unreachable_google_is_not_a_verdict_on_the_grant(monkeypatch, tmp_path):
    """A laptop with no network must not be told to re-authorize."""
    from google.auth.exceptions import RefreshError, TransportError

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=PAST)

    def offline(*_a, **_k):
        raise TransportError("Connection refused")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", offline)
    assert auth.health("a@gmail.com") == "configured"

    def busy(*_a, **_k):
        raise RefreshError("internal_failure", retryable=True)

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", busy)
    assert auth.health("a@gmail.com") == "configured"


def test_health_never_raises_on_an_unreadable_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=FUTURE)

    def denied(*_a, **_k):
        raise PermissionError("permission denied")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.from_authorized_user_file", denied)
    assert auth.health("a@gmail.com") == "configured"


def test_a_token_that_cannot_renew_itself_is_expired(monkeypatch, tmp_path):
    """An access token alone dies within the hour, so it counts as expired
    while it still works."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=FUTURE, refresh_token=None)
    assert auth.health("a@gmail.com") == "expired"

    _authorized_user(tmp_path, "b@gmail.com", expiry=FUTURE, refresh_token="")
    assert auth.health("b@gmail.com") == "expired"


def test_health_writes_back_what_it_refreshed(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    path = _authorized_user(tmp_path, "a@gmail.com", expiry=PAST)

    def refreshed(self, _request):
        self.token = "fresh"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refreshed)
    assert auth.health("a@gmail.com") == "ok"
    assert json.loads(path.read_text())["token"] == "fresh"


def test_client_health_checks_the_file_the_client_would_read(monkeypatch, tmp_path):
    """health() on a client with an overridden token_file() reads that file."""
    from gmailscan import GmailClient

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path / "discovered"))
    legacy = _authorized_user(tmp_path / "legacy", "a@gmail.com", expiry=FUTURE)

    class _Legacy(GmailClient):
        def token_file(self):
            return legacy

    assert auth.health("a@gmail.com") == "missing"
    assert _Legacy("a@gmail.com").health() == "ok"


def test_persist_token_survives_a_read_only_store(monkeypatch, tmp_path, caplog):
    """A read-only mount must not fail the run; the credential still works."""
    path = tmp_path / "ro" / "token.json"

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    auth.persist_token(path, "{}")  # must not raise
    assert "could not persist" in caplog.text.lower()


def test_persist_token_makes_the_file_owner_only(tmp_path):
    """A refreshed token is a credential; the write-back must not leave it
    world-readable even when the file already was."""
    path = tmp_path / "token.json"
    path.touch()
    path.chmod(0o644)
    auth.persist_token(path, "{}")
    assert path.stat().st_mode & 0o777 == 0o600


# ------------------------------------------------------- grant-age reporting


def test_granted_marker_is_not_mistaken_for_an_account(monkeypatch, tmp_path):
    """The sidecar sits next to the token in the same directory."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    (tmp_path / "token-a@gmail.com.json.granted").write_text("2026-08-31T00:00:00+00:00")
    assert auth.authorized_accounts() == ["a@gmail.com"]


def test_grant_age_does_not_come_from_the_token_mtime(monkeypatch, tmp_path):
    """persist_token rewrites the token on every access-token refresh, so the
    mtime would report 0d ago forever."""
    from datetime import datetime, timedelta, timezone

    from gmailscan import cli

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    path = _write_token(tmp_path, "a@gmail.com")
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    cli._granted_marker(path).write_text(long_ago.isoformat())

    # Simulate a refresh: the token file is rewritten right now.
    auth.persist_token(path, '{"refresh_token": "r"}')

    reported = cli._granted_age(path)
    assert "30d ago" in reported
    assert "outlived" in reported  # past 7 days, so the app is published


def test_grant_age_says_so_when_unknown(monkeypatch, tmp_path):
    from gmailscan import cli

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    path = _write_token(tmp_path, "a@gmail.com")
    assert "unknown" in cli._granted_age(path)


def test_declared_version_matches_the_package_metadata():
    """pyproject and __version__ have drifted before; an image then reports a
    version that never existed."""
    from importlib.metadata import version

    import gmailscan

    assert version("gmailscan") == gmailscan.__version__


def test_every_public_name_resolves():
    """A stale entry in __all__ breaks ``from gmailscan import *``, and the
    names the README uses have to be in it."""
    import gmailscan

    for name in gmailscan.__all__:
        assert hasattr(gmailscan, name), name
    for name in ("search_all", "authorized_accounts", "health", "health_detail", "is_configured"):
        assert name in gmailscan.__all__, name
    for name in ("GmailClient", "EmailMessage", "MessageRef", "GmailAuthRequired"):
        assert name in gmailscan.__all__, name


def test_the_readme_tells_cli_users_to_take_both_extras():
    """--account needs the auth extra and --push the secrets extra; the README
    has to say so in one place."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "gmailscan[auth,secrets]" in readme


def test_the_readme_pin_example_names_the_current_release():
    """The pin examples and the sample log line must name the current release."""
    from pathlib import Path

    import gmailscan

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    v = gmailscan.__version__
    assert f"gmailscan@v{v}" in readme
    assert f"gmailscan {v} reading" in readme
    assert f"releases/download/v{v}/gmailscan-{v}-py3-none-any.whl" in readme


# ------------------------------------- a grant that has never had an access token


def test_load_credentials_refreshes_a_token_that_was_never_used(monkeypatch, tmp_path):
    """A file holding a refresh token but no access token is refreshed, the
    same as an expired one."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=None, token=None)

    def refreshed(self, _request):
        self.token = "fresh"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refreshed)
    assert auth.load_credentials("a@gmail.com").token == "fresh"


def test_health_detail_carries_googles_reason(monkeypatch, tmp_path):
    from google.auth.exceptions import RefreshError

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=PAST)

    def refused(*_a, **_k):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refused)
    assert auth.health_detail("a@gmail.com") == (
        "revoked",
        "invalid_grant: Token has been expired or revoked.",
    )
    assert auth.health_detail("nobody@gmail.com")[0] == "missing"


# ---------------------------------------------------------------- --status


def _fake_health(verdicts):
    def health_detail(account, *, path=None):
        return verdicts[account]

    return health_detail


def test_status_reports_healths_verdict_and_fails_on_a_dead_grant(monkeypatch, tmp_path, capsys):
    """--status prints health()'s verdict, so the CLI and a status screen agree."""
    from gmailscan import cli

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "alive@gmail.com")
    _write_token(tmp_path, "dead@gmail.com")
    monkeypatch.setattr(
        cli,
        "health_detail",
        _fake_health(
            {
                "alive@gmail.com": ("ok", "refreshed"),
                "dead@gmail.com": ("revoked", "invalid_grant: Token has been expired or revoked."),
            }
        ),
    )

    assert cli.main(["--status"]) == 1
    out = capsys.readouterr().out
    assert "alive@gmail.com" in out and "OK" in out
    assert "dead@gmail.com" in out and "REVOKED" in out and "invalid_grant" in out
    assert "1 account(s) need re-authorizing" in out


def test_status_json_carries_the_status(monkeypatch, tmp_path, capsys):
    import json as _json

    from gmailscan import cli

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    monkeypatch.setattr(cli, "health_detail", _fake_health({"a@gmail.com": ("ok", "valid")}))

    assert cli.main(["--status", "--json"]) == 0
    report = _json.loads(capsys.readouterr().out)
    assert report["accounts"] == ["a@gmail.com"]
    assert report["health"]["a@gmail.com"]["state"] == "ok"
    assert "granted" in report["health"]["a@gmail.com"]


def test_an_unreachable_google_is_not_a_reauth_order_in_status(monkeypatch, tmp_path, capsys):
    from gmailscan import cli

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    monkeypatch.setattr(
        cli,
        "health_detail",
        _fake_health({"a@gmail.com": ("configured", "could not reach Google")}),
    )
    assert cli.main(["--status"]) == 1  # not healthy, so not 0
    out = capsys.readouterr().out
    assert "CONFIGURED" in out
    assert "need re-authorizing" not in out


# --------------------------------------------- the rest of load_credentials


def test_token_dir_is_the_canonical_store_when_nothing_overrides_it(monkeypatch):
    monkeypatch.delenv("GMAILSCAN_TOKEN_DIR", raising=False)
    assert auth.token_dir() == auth.DEFAULT_TOKEN_DIR


def test_load_credentials_explains_an_unusable_token_file(monkeypatch, tmp_path):
    """google-auth rejects a file without a refresh token; the error carries
    the path and the fix."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=FUTURE, refresh_token=None)
    with pytest.raises(auth.GmailAuthRequired) as excinfo:
        auth.load_credentials("a@gmail.com")
    message = str(excinfo.value)
    assert "token-a@gmail.com.json" in message and "gmailscan-auth" in message


def test_load_credentials_refuses_a_token_that_cannot_renew_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=PAST, refresh_token="")
    with pytest.raises(auth.GmailAuthRequired, match="no refresh token"):
        auth.load_credentials("a@gmail.com")


def test_load_credentials_turns_a_refused_refresh_into_the_fix(monkeypatch, tmp_path):
    from google.auth.exceptions import RefreshError

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=PAST)

    def refused(*_a, **_k):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", refused)
    with pytest.raises(auth.GmailAuthRequired) as excinfo:
        auth.load_credentials("a@gmail.com")
    assert "invalid_grant" in str(excinfo.value)
    assert "7 days" in str(excinfo.value)  # the usual cause, named


def test_health_without_google_auth_is_configured_not_a_crash(monkeypatch, tmp_path):
    """Without google-auth, health() reports that a token exists and nothing
    more."""
    import sys

    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _authorized_user(tmp_path, "a@gmail.com", expiry=FUTURE)
    monkeypatch.setitem(sys.modules, "google.auth.exceptions", None)  # the import raises
    assert auth.health_detail("a@gmail.com") == (
        "configured",
        "google-auth is not installed, so the grant cannot be checked",
    )


def test_load_credentials_hands_back_a_live_token_without_touching_google(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    path = _authorized_user(tmp_path, "a@gmail.com", expiry=FUTURE)
    before = path.read_text()

    def no_network(*_a, **_k):
        raise AssertionError("a valid access token needs no refresh")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", no_network)
    assert auth.load_credentials("a@gmail.com").token == "access"
    assert path.read_text() == before  # nothing to write back
