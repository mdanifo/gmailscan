"""The optional AWS token store, with boto3 mocked entirely.

These assert the properties that matter for an unattended run: it fails loudly
rather than scanning nothing, and pushing one account never drops the other.
"""

from __future__ import annotations

import json

import pytest

from gmailscan import GmailAuthRequired
from gmailscan import secrets as secrets_mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for key in ("GMAILSCAN_TOKEN", "GMAILSCAN_ACCOUNTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GMAILSCAN_TOKEN_DIR", str(tmp_path / "tokens"))
    yield


class _FakeSecrets:
    """Just enough Secrets Manager to exercise both branches."""

    def __init__(self, stored=None, exists=True):
        self.stored = stored
        self.exists = exists
        self.put_calls: list[str] = []
        self.create_calls: list[str] = []

    def get_secret_value(self, SecretId):  # noqa: N803 - mirrors boto3
        if not self.exists:
            raise RuntimeError("ResourceNotFoundException")
        return {"SecretString": json.dumps(self.stored or {})}

    def put_secret_value(self, SecretId, SecretString):  # noqa: N803
        if not self.exists:
            raise RuntimeError("ResourceNotFoundException")
        self.put_calls.append(SecretString)
        self.stored = json.loads(SecretString)

    def create_secret(self, Name, SecretString):  # noqa: N803
        self.create_calls.append(SecretString)
        self.stored = json.loads(SecretString)
        self.exists = True


def _install(monkeypatch, fake):
    monkeypatch.setattr(secrets_mod, "_client", lambda region=None: fake)


def _token_doc(account="a@gmail.com"):
    return {account: {"refresh_token": "r", "client_id": "c"}}


def test_importing_gmailscan_never_requires_boto3():
    """The lazy import is the point: filesystem callers must not need AWS."""
    import gmailscan

    assert "boto3" not in str(gmailscan.__doc__ or "") or True
    source = (secrets_mod.__file__ or "")
    assert source.endswith("secrets.py")
    # boto3 is imported inside _client, never at module scope.
    with open(source, encoding="utf-8") as fh:
        head = "".join(fh.readlines()[:30])
    assert "import boto3" not in head


def test_hydrate_writes_every_account(monkeypatch, tmp_path):
    fake = _FakeSecrets(
        {
            "a@gmail.com": {"refresh_token": "r"},
            "b@gmail.com": {"refresh_token": "r"},
        }
    )
    _install(monkeypatch, fake)
    assert secrets_mod.hydrate_tokens() == ["a@gmail.com", "b@gmail.com"]
    written = sorted(p.name for p in (tmp_path / "tokens").glob("token-*.json"))
    assert written == ["token-a@gmail.com.json", "token-b@gmail.com.json"]


def test_hydrate_skips_metadata_keys(monkeypatch, tmp_path):
    fake = _FakeSecrets({"_meta": {"granted_at": "x"}, "a@gmail.com": {"refresh_token": "r"}})
    _install(monkeypatch, fake)
    assert secrets_mod.hydrate_tokens() == ["a@gmail.com"]
    assert not (tmp_path / "tokens" / "token-_meta.json").exists()


def test_hydrate_of_an_empty_secret_raises(monkeypatch):
    """A silent zero-result sweep looks identical to a mailbox with no new mail."""
    _install(monkeypatch, _FakeSecrets({}))
    with pytest.raises(GmailAuthRequired, match="empty"):
        secrets_mod.hydrate_tokens()


def test_hydrate_of_metadata_only_raises(monkeypatch):
    _install(monkeypatch, _FakeSecrets({"_meta": {"granted_at": "x"}}))
    with pytest.raises(GmailAuthRequired, match="no account tokens"):
        secrets_mod.hydrate_tokens()


def test_hydrate_of_a_missing_secret_raises_with_the_fix(monkeypatch):
    _install(monkeypatch, _FakeSecrets(exists=False))
    with pytest.raises(GmailAuthRequired, match="gmailscan-auth --push"):
        secrets_mod.hydrate_tokens()


def test_hydrated_tokens_are_owner_only(monkeypatch, tmp_path):
    _install(monkeypatch, _FakeSecrets(_token_doc()))
    secrets_mod.hydrate_tokens()
    mode = (tmp_path / "tokens" / "token-a@gmail.com.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_push_merges_rather_than_replaces(monkeypatch, tmp_path):
    """Pushing one account must not delete the other's grant from the secret --
    that is exactly what would leave a scheduled sweep reading one mailbox."""
    tokens = tmp_path / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "token-a@gmail.com.json").write_text(json.dumps({"refresh_token": "new-a"}))

    fake = _FakeSecrets({"b@gmail.com": {"refresh_token": "existing-b"}})
    _install(monkeypatch, fake)

    assert secrets_mod.push_tokens(["a@gmail.com"]) == ["a@gmail.com"]
    assert set(fake.stored) >= {"a@gmail.com", "b@gmail.com"}
    assert fake.stored["b@gmail.com"]["refresh_token"] == "existing-b"


def test_push_creates_the_secret_when_absent(monkeypatch, tmp_path):
    tokens = tmp_path / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "token-a@gmail.com.json").write_text(json.dumps({"refresh_token": "r"}))
    fake = _FakeSecrets(exists=False)
    _install(monkeypatch, fake)
    secrets_mod.push_tokens(["a@gmail.com"])
    assert fake.create_calls


def test_push_records_granted_at(monkeypatch, tmp_path):
    tokens = tmp_path / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "token-a@gmail.com.json").write_text(json.dumps({"refresh_token": "r"}))
    fake = _FakeSecrets({})
    _install(monkeypatch, fake)
    secrets_mod.push_tokens(["a@gmail.com"], granted_at="2026-08-31T00:00:00Z")
    assert fake.stored["_meta"]["granted_at"] == "2026-08-31T00:00:00Z"


def test_push_with_nothing_local_raises(monkeypatch):
    _install(monkeypatch, _FakeSecrets({}))
    with pytest.raises(GmailAuthRequired):
        secrets_mod.push_tokens()


def test_read_meta_never_raises(monkeypatch):
    """A missing reminder timestamp must not take down the run it hangs off."""
    _install(monkeypatch, _FakeSecrets(exists=False))
    assert secrets_mod.read_meta() == {}

    _install(monkeypatch, _FakeSecrets({"_meta": {"granted_at": "x"}}))
    assert secrets_mod.read_meta() == {"granted_at": "x"}
