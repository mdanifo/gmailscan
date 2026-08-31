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


def test_no_longer_falls_back_into_another_repo(monkeypatch, tmp_path):
    """The old clients fell back to virtual-closet/.sessions/, which drifted.

    A path into a sibling checkout cannot be installed, pinned or deployed, and
    the copies there were weeks out of date.
    """
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    resolved = str(auth.token_path("someone@gmail.com"))
    assert ".sessions" not in resolved
    assert "virtual-closet" not in resolved


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
    assert auth.token_path("MDanifo@Gmail.com") == auth.token_path("mdanifo@gmail.com")


def test_authorized_accounts_discovers_every_mailbox(monkeypatch, tmp_path):
    """This is what makes scanning both accounts the default."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "mdanifo@gmail.com")
    _write_token(tmp_path, "mdanifo100@gmail.com")
    assert auth.authorized_accounts() == ["mdanifo100@gmail.com", "mdanifo@gmail.com"]


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


def test_persist_token_survives_a_read_only_store(monkeypatch, tmp_path, caplog):
    """A read-only mount must not fail the run; the credential still works."""
    path = tmp_path / "ro" / "token.json"

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    auth.persist_token(path, "{}")  # must not raise
    assert "could not persist" in caplog.text.lower()


# ------------------------------------------------------- grant-age reporting


def test_granted_marker_is_not_mistaken_for_an_account(monkeypatch, tmp_path):
    """The sidecar sits next to the token in the same directory."""
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path))
    _write_token(tmp_path, "a@gmail.com")
    (tmp_path / "token-a@gmail.com.json.granted").write_text("2026-08-31T00:00:00+00:00")
    assert auth.authorized_accounts() == ["a@gmail.com"]


def test_grant_age_does_not_come_from_the_token_mtime(monkeypatch, tmp_path):
    """persist_token rewrites the token on every access-token refresh, so mtime
    resets to today the moment anything reads the mailbox -- it would report
    "granted 0d ago" forever, which is the one number this has to get right."""
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
