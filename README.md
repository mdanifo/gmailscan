# gmailscan

Read-only Gmail access shared by the personal projects (`amazon-ledger`,
`open-job-aggregator`, `virtual-closet`): one token store, one client, one
consent command.

## Install

There is no PyPI package. Pin a release wheel, or a tag:

```bash
pip install "gmailscan @ https://github.com/mdanifo/gmailscan/releases/download/v0.5.0/gmailscan-0.5.0-py3-none-any.whl"
pip install "gmailscan @ git+https://github.com/mdanifo/gmailscan@v0.5.0"
```

The wheel installs into a `python:slim` image without git. Read
[CHANGELOG.md](CHANGELOG.md) before deciding a bump can wait.

Extras:

- `gmailscan[secrets]`: `boto3`, for hydrating tokens from AWS Secrets Manager
  in unattended runs.
- `gmailscan[auth]`: `google-auth-oauthlib`, for the consent flow.
- `gmailscan[auth,secrets]`: what the `gmailscan-auth` command needs.
  `--account` uses one extra and `--push` the other, and a missing one only
  fails at the moment you reach for it.

A project that only reads mail needs neither extra.

Each client logs one line when built, `gmailscan 0.5.0 reading <address>`, so
a consumer's own logs say which release it runs. To catch an environment that
is not running the pin, add this to the consumer's suite, pointed at whichever
file holds the pin:

```python
def test_the_gmailscan_pin_is_what_is_installed():
    import re
    from pathlib import Path

    import gmailscan

    pins = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    # Either form: the wheel (gmailscan-X.Y.Z-py3-none-any.whl) or the tag (@vX.Y.Z).
    pinned = re.search(r"gmailscan(?:-|@v)(\d+\.\d+\.\d+)", pins)
    assert pinned, "no gmailscan wheel or tag pin found"
    assert gmailscan.__version__ == pinned.group(1), (
        f"installed gmailscan {gmailscan.__version__}, pinned {pinned.group(1)}: "
        "reinstall, or bump the pin"
    )
```

`__version__` matches the tag from v0.1.5 on; earlier tags all report `0.1.0`.

The package ships `py.typed` and passes strict mypy, so a consumer needs no
`ignore_missing_imports` override for `gmailscan.*`.

## Scope

`gmail.readonly` only. The package can search and read mail and cannot send,
modify, label or delete it. `tests/test_auth.py` pins the scope.

## Tokens

Tokens are cached per account at `~/.config/google-oauth/token-<address>.json`.
Lookup order, first hit wins:

1. `GMAILSCAN_TOKEN`: one explicit file, for a single-account caller
2. `GMAILSCAN_TOKEN_DIR`: a directory of `token-<address>.json`
3. `~/.config/google-oauth/`, the default

`GMAILSCAN_ACCOUNTS` (comma-separated) pins which mailboxes are read. Unset,
every mailbox with a token is read.

## Use

```python
from datetime import date
from gmailscan import search_all, authorized_accounts

# Every authorized mailbox, each hit tagged with the one it came from.
for msg in search_all("from:shipment-tracking@amazon.com", after=date(2026, 8, 1)):
    print(msg.account, msg.subject, msg.html_first[:80])

# Or pin to one mailbox.
for msg in search_all("subject:interview", accounts=["you@gmail.com"]):
    print(msg.text_first)
```

`EmailMessage` has no `body`. Pick `text_first` (recruiter prose) or
`html_first` (order tables); each falls back to the other part. `received` is
Gmail's `internalDate` as a UTC `datetime` and is the field to sort or window
by; `date` is the raw `Date` header. `received` is `None` on a hand-built
fixture.

### Is the grant alive?

`is_configured()` only checks that a token file exists, and a revoked grant
leaves its file behind. `health()` refreshes the token to find out:

```python
from gmailscan import health

health("you@gmail.com")  # "ok", "revoked", "expired", "configured" or "missing"
```

`revoked` covers Testing-mode expiry too; Google reports both as
`invalid_grant`. `configured` means a token exists and nothing more could be
learned (no network, say) and is not a reason to re-authorize. A consumer that
overrides `GmailClient.token_file()` should call `client.health()` so the check
reads the same file. `health_detail()` adds Google's reason.

## Quota

Gmail allows 6,000 quota units per user per minute, per Google Cloud project
([Google's table](https://developers.google.com/workspace/gmail/api/reference/quota)).
What this package spends:

| call            | units | made by                                                |
| --------------- | ----- | ------------------------------------------------------ |
| `messages.list` | 5     | `search()` and `list_ids()`, per page of up to 500 ids |
| `messages.get`  | 20    | `search()` per message; `get_message()`; `raw()`       |
| `threads.get`   | 40    | `get_thread()`                                         |

So one mailbox yields about 300 messages a minute. A larger sweep is refused
partway through and waits for the minute to turn over: up to nine attempts,
each wait capped at 90 seconds, `Retry-After` honoured when Google sends one
(clamped to 120 seconds). A real 403 (revoked grant, wrong scope) is not
retried.

Keeping a sweep cheap:

- `after=` and `before=` narrow the query on Google's side. Sweep since the
  last run, not over a fixed window.
- Do not re-read what you have already processed. Pass the ids you have
  finished with as `search(skip_ids=...)`: they are listed but not read, and
  do not count toward `limit`. Or call `list_ids()` (5 units for up to 500
  ids) and `get_message()` only what is new.
- `list_ids()` also tells you which threads had mail in the window, from the
  `threadId` on each ref, for less than a 40-unit `get_thread()`.
- `headers_only=True` saves bandwidth. A metadata get costs the same 20 units
  as a full one.
- `limit` is per mailbox and a hard stop. `search(limit=200)` is about 4,000
  units.
- Every project here reads through the same OAuth client, so they share each
  mailbox's 6,000 units. Two jobs on one mailbox in the same minute both wait
  longer.

Messages are fetched one at a time. Batching `messages.get` is faster per call,
but Google counts a batch of n as n calls, so it hits the same ceiling sooner
and then needs per-call retries. Nothing here is waiting on fetch latency.

## CLI

```bash
gmailscan-auth --status                 # what is authorized, and does it still work?
gmailscan-auth --account you@gmail.com --manual --client-secret-file ~/client.json
gmailscan-auth --push                   # copy local tokens into AWS Secrets Manager
```

`--status` is the first thing to run when a sweep finds nothing:

```
token directory: ~/.config/google-oauth

  other@gmail.com         REVOKED     invalid_grant: Token has been expired or revoked.
  you@gmail.com            OK          granted 32d ago (2026-09-01)   <-- outlived Testing's 7 days
```

The status column is `health()`'s verdict. Exit status is non-zero unless
every grant is `OK`. `--status --json` prints the same report as JSON, with a
`health` key per account.

On a headless box use `--manual`. There is no local listener: the browser's
redirect fails to load, but the address bar still carries `?code=...`, and
pasting that whole URL back completes the exchange. Paste it into the same
process; the PKCE verifier lives on the flow object, so a second run cannot
finish the first run's consent.

## Publish the OAuth app

While the consent screen is in Testing, Google revokes the refresh token after
7 days and every account dies weekly with `invalid_grant: Bad Request`. Adding
a test user does not change that. Fix it once in Google Cloud Console: APIs &
Services, OAuth consent screen, Audience, Publish app. Then run
`gmailscan-auth --account <address> --force` to mint a long-lived token.

## Unattended runs

A Lambda or scheduled container has no token file and cannot open a browser.
Push the grant once from the machine that consented, then hydrate it at
startup into a writable directory:

```bash
gmailscan-auth --push
```

```python
import os, tempfile
os.environ["GMAILSCAN_TOKEN_DIR"] = tempfile.mkdtemp()
from gmailscan.secrets import hydrate_tokens
hydrate_tokens()
```

`hydrate_tokens` raises on an empty or metadata-only secret instead of letting
the run sweep nothing.

## Tests

```bash
pip install -e ".[dev]"
ruff check src tests scripts && ruff format --check src tests scripts && mypy && pytest --cov
```

CI runs the same on every pull request, on Python 3.10, 3.12 and 3.14, and
fails under 95% coverage. Nothing in the suite touches the network. Three tiers
run by default:

- unit (`test_auth.py`, `test_client.py`, `test_secrets.py`, `test_cli.py`):
  the Gmail API, google-auth and boto3 faked one object at a time
- component (`pytest -m component`): `gmailscan-auth` run as a subprocess,
  checking exit status and output
- end to end (`pytest -m e2e`): the real token file, google-auth refresh,
  discovery client and httplib2 against the fake Google in
  `tests/conftest.py`, quota refusals and a revoked grant included

## Releasing

1. Bump `version` in `pyproject.toml` and `__version__`, and write the
   CHANGELOG entry. The suite fails if the three disagree.
2. Tag and push: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`.

The Release workflow runs the suite on the tagged commit, checks the tag
against `__version__` and the CHANGELOG, builds a wheel and sdist, and
publishes the GitHub release with that version's CHANGELOG entry as its notes.
