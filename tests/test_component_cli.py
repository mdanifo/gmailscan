"""Component tier: the installed command, in its own process.

The unit tests call cli.main() directly. These run ``gmailscan-auth`` the way a
shell or a cron line does -- through the console-script entry point, with
environment variables, reading its stdout and exit status -- so what is being
checked is the packaging and the process contract, not the functions behind
them. No network: the token in the temp store is valid, so nothing refreshes.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.component

FUTURE = "2099-01-01T00:00:00Z"


def _run(*args: str, token_dir) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GMAILSCAN_TOKEN_DIR": str(token_dir)}
    env.pop("GMAILSCAN_TOKEN", None)
    env.pop("GMAILSCAN_ACCOUNTS", None)
    return subprocess.run(
        [sys.executable, "-m", "gmailscan.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _live_token(directory, account: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"token-{account}.json").write_text(
        json.dumps(
            {
                "token": "access",
                "refresh_token": "refresh",
                "client_id": "c",
                "client_secret": "s",
                "token_uri": "https://oauth2.googleapis.com/token",
                "expiry": FUTURE,
            }
        )
    )


def test_the_console_script_is_wired_to_main():
    """pip install creates `gmailscan-auth` from this entry point; if the
    target moves, every README command breaks at once."""
    (entry,) = [
        e
        for e in importlib.metadata.entry_points(group="console_scripts")
        if e.name == "gmailscan-auth"
    ]
    assert entry.value == "gmailscan.cli:main"
    assert entry.load() is importlib.import_module("gmailscan.cli").main


def test_status_exit_status_says_whether_every_grant_is_ok(tmp_path):
    empty = _run("--status", token_dir=tmp_path / "none")
    assert empty.returncode == 1
    assert "No Gmail account is authorized here" in empty.stdout
    assert "gmailscan-auth --account" in empty.stdout  # the fix, not just the fact

    _live_token(tmp_path, "a@gmail.com")
    live = _run("--status", token_dir=tmp_path)
    assert live.returncode == 0, live.stderr
    assert "a@gmail.com" in live.stdout and " OK " in live.stdout
    assert f"token directory: {tmp_path}" in live.stdout


def test_status_json_is_parseable_and_complete(tmp_path):
    _live_token(tmp_path, "a@gmail.com")
    result = _run("--status", "--json", token_dir=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["token_dir"] == str(tmp_path)
    assert report["accounts"] == ["a@gmail.com"]
    assert report["health"]["a@gmail.com"]["state"] == "ok"
    assert set(report["health"]["a@gmail.com"]) == {"state", "detail", "granted"}


def test_no_arguments_means_status(tmp_path):
    _live_token(tmp_path, "a@gmail.com")
    assert _run(token_dir=tmp_path).returncode == 0


def test_authorizing_without_a_client_secret_explains_the_one_time_setup(tmp_path):
    result = _run("--account", "new@gmail.com", token_dir=tmp_path)
    assert result.returncode == 2
    assert "client-secret-file" in result.stderr
    assert "PUBLISH THE APP TO PRODUCTION" in result.stderr


def test_an_already_authorized_account_is_not_re_consented_without_force(tmp_path):
    _live_token(tmp_path, "a@gmail.com")
    result = _run("--account", "A@Gmail.com", token_dir=tmp_path)
    assert result.returncode == 0
    assert "already has a token" in result.stdout
    assert "--force" in result.stdout


def test_help_names_every_command_the_readme_documents():
    result = subprocess.run(
        [sys.executable, "-m", "gmailscan.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    for flag in (
        "--status",
        "--account",
        "--manual",
        "--client-secret-file",
        "--push",
        "--json",
        "--force",
    ):
        assert flag in result.stdout
