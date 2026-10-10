# gmailscan

Shared **read-only** Gmail access for the personal projects (`amazon-ledger`,
`open-job-aggregator`, `virtual-closet`, …).

Before this, three projects each carried their own Gmail client: three token
lookup orders, two of which fell back to reading `virtual-closet/.sessions/` in
a sibling checkout, and two separately written consent scripts that had drifted
in flags and defaults. Only one of the three scanned both mailboxes. This is the
single canonical version.

## Install

Pin it from git (no PyPI):

```bash
pip install "gmailscan @ git+https://github.com/mdanifo/gmailscan"
# unattended runs that hydrate tokens from AWS Secrets Manager:
pip install "gmailscan[secrets] @ git+https://github.com/mdanifo/gmailscan"
# running the consent flow on this machine:
pip install "gmailscan[auth] @ git+https://github.com/mdanifo/gmailscan"
# running gmailscan-auth yourself: you want BOTH, or --push fails at the
# point of use with "needs the secrets extra" long after install
pip install "gmailscan[auth,secrets] @ git+https://github.com/mdanifo/gmailscan"
```

`boto3` is only needed for the AWS token store and `google-auth-oauthlib` only
for the consent flow — a project that just reads mail needs neither.

**Installing the CLI, though, take `[auth,secrets]`.** The extras are split so a
library consumer stays lean, but `gmailscan-auth` uses both: `--account` needs
`auth`, `--push` needs `secrets`. Installing one leaves the other failing weeks
later at the moment you reach for it, with the install long forgotten.

A project should pin a tag, `gmailscan @ git+https://github.com/mdanifo/gmailscan@v0.4.1`,
and read [CHANGELOG.md](CHANGELOG.md) before deciding a bump can wait.

Every release also carries a wheel. An image should install that: pip alone
does it, where the git+https form needs git installed and purged again.

```bash
pip install "gmailscan @ https://github.com/mdanifo/gmailscan/releases/download/v0.4.1/gmailscan-0.4.1-py3-none-any.whl"
```

### Check the pin in your own tests

A pin protects you from surprises and from fixes alike. jobpipe sat on v0.1.3
through five releases, including the one that fixed its own four-day Gmail
outage, and nothing said so. Copy this into the consumer's suite, pointed at
whichever file holds the pin:

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

It fails when the pin and the environment disagree (the pin bumped and the venv
never reinstalled, or the reverse), so old shared code fails your tests instead
of your sweep. It cannot tell you the pin itself has fallen behind. The
CHANGELOG can, and so can the line every client logs when it is built:
`gmailscan 0.4.1 reading <address>`.

`__version__` has matched the tag since v0.1.5. Earlier tags all report `0.1.0`,
so this check means nothing below v0.1.5.

The package ships `py.typed` and passes strict mypy, so a consumer needs no
`ignore_missing_imports` override for `gmailscan.*` and no `type: ignore` on a
`GmailClient` subclass.

## Scope

`gmail.readonly` and nothing else. This package can search and read mail; it
cannot send, modify, label, or delete it. That is structural rather than a
promise, and the test suite pins it.

## Use

```python
from datetime import date
from gmailscan import search_all, authorized_accounts

# Every authorized mailbox, each hit tagged with the one it came from.
for msg in search_all("from:shipment-tracking@amazon.com", after=date(2026, 8, 1)):
    print(msg.account, msg.subject, msg.html_first[:80])

# Or pin to one mailbox.
for msg in search_all("subject:interview", accounts=["mdanifo@gmail.com"]):
    print(msg.text_first)
```

### `text_first` / `html_first`, and why there is no `body`

The two clients this was extracted from disagreed about what `body` meant, and
both were right for their own mail. Recruiter correspondence carries its detail
in prose, so plain text is the signal and the HTML twin is the same words in
markup that only costs tokens. Order confirmations carry their detail in tables,
so the HTML is the signal and the text part is a lossy summary.

Shipping either as `body` would have silently changed what one project reads
without changing a line of its code, so callers say which they want.

### `received`, and why not `date`

`date` is the `Date` header: whatever the sender's machine wrote, in its own
time zone and occasionally its own language. `received` is Gmail's own receipt
timestamp (`internalDate`) as a UTC `datetime`, and is the one to sort or
window by. It is `None` on a hand-built fixture.

### Is the grant alive?

`is_configured()` checks that a token file exists. A revoked grant leaves its
file behind, so it keeps answering `True` for a mailbox that can no longer be
read. It stays a file check because callers rely on it never touching the
network. `health()` refreshes to find out:

```python
from gmailscan import health

health("mdanifo@gmail.com")  # "ok", "revoked", "expired", "configured" or "missing"
```

`revoked` covers Testing-mode expiry too; Google reports both as
`invalid_grant`. `configured` means a token exists and nothing more could be
learned, such as with no network. It is not a reason to re-authorize. A
consumer that overrides `GmailClient.token_file()` should call `client.health()`
so the check reads the file the client would. `health_detail()` returns the
same verdict with Google's reason attached, for a status screen.

## Quota

Gmail meters **quota units per user per minute**: 6,000 for each mailbox, per
Google Cloud project
([Google's table](https://developers.google.com/workspace/gmail/api/reference/quota),
as of September 2026). What this package spends:

| call            | units | made by                                     |
| --------------- | ----- | ------------------------------------------- |
| `messages.list` | 5     | `search()`, once per 100 hits               |
| `messages.get`  | 20    | `search()`, once per message; `raw()`       |
| `threads.get`   | 40    | `get_thread()`                              |

So one mailbox yields at most **300 messages a minute**, and fetching them one
at a time already runs a little faster than that (about 330 a minute,
measured). A sweep of more than a few hundred messages will be refused partway
through, and that is expected rather than an error. The refusal clears when the
minute turns over, and every request waits it out: up to nine attempts, each
wait capped at 90 seconds, `Retry-After` honoured when Google sends one. A
backoff that gives up inside sixty seconds gives up just before the window
resets, which is what v0.2.0's did. A real 403 (revoked grant, wrong scope) is
never retried.

Sizing a sweep:

- `search(limit=200)` costs about 4,000 units, two-thirds of a minute on its
  own. `limit` is per mailbox and a hard stop.
- `after=` and `before=` narrow the query on Google's side and are the cheapest
  saving there is: sweep since the last run, not over a fixed window.
- `headers_only=True` saves bandwidth, not quota. A metadata get costs the same
  20 units as a full one; it downloads kilobytes instead of megabytes.
- A thread read costs two message reads. Reading every thread a search turned
  up triples the bill for those messages.
- Every project here reads through the same OAuth client, so they share each
  mailbox's 6,000. Two jobs reading one mailbox in the same minute split it,
  and both wait longer.

**Why messages are not batched.** Measured 2026-09-13: batching `messages.get`
is 3 to 10 times faster, but batches of 50 to 100 had up to a third of their
calls refused for rate limiting, each of which needs its own retry. Google counts
a batch of n calls as n calls, so batching cannot lift the 300-a-minute ceiling;
it only reaches it sooner. No consumer here is waiting on fetch latency, so
requests stay one at a time.

## Where tokens live

One canonical store: `~/.config/google-oauth/token-<address>.json`.

Resolution order, first hit wins:

1. `GMAILSCAN_TOKEN` — one explicit file, for a single-account caller
2. `GMAILSCAN_TOKEN_DIR` — a directory of `token-<address>.json`
3. `~/.config/google-oauth/` — the canonical store

`GMAILSCAN_ACCOUNTS` (comma-separated) pins which mailboxes are scanned. Unset,
every authorized mailbox is discovered — that is what makes scanning both the
default rather than something each project reimplements.

## CLI

```bash
gmailscan-auth --status                 # what is authorized, and does it still work?
gmailscan-auth --account you@gmail.com --manual --client-secret-file ~/client.json
gmailscan-auth --push                   # copy local tokens into AWS Secrets Manager
```

`--status` is the one to reach for first. It answers the question behind almost
every "why did the sweep find nothing" investigation:

```
token directory: /home/mike/.config/google-oauth

  mdanifo100@gmail.com         REVOKED     invalid_grant: Token has been expired or revoked.
  mdanifo@gmail.com            OK          granted 32d ago (2026-08-31)   <-- outlived Testing's 7 days
```

The status column is `health()`'s verdict, so it cannot disagree with what a
consumer's own status check says. The exit status is non-zero unless every
grant is `OK`. `--status --json` prints the same report as a document, with a
`health` key per account, for a cron job or a dashboard to read.

**On a headless box use `--manual`.** There is no local listener and no port
forward: the browser's redirect fails to load, but the address bar still carries
`?code=...`, and pasting that whole URL back completes the exchange. It must go
into *that same process* — the PKCE verifier lives on the flow object and is
never sent to Google, so a second run cannot finish the first run's consent.

## Publish the OAuth app to production

> While the consent screen is in **Testing**, Google revokes the refresh token
> after **7 days**, so every account dies weekly with
> `invalid_grant: Bad Request`. Fix it once: Google Cloud Console → *APIs &
> Services → OAuth consent screen* → **Audience** → **Publish app**. Adding a
> *test user* does **not** stop the 7-day expiry; only production status does.
> After publishing, re-run `gmailscan-auth --account <address> --force` once to
> mint a long-lived token.

This is the single most common cause of these projects silently reading nothing,
and no amount of re-authorizing outlasts it.

## Unattended runs

A Lambda or scheduled container has no token file and cannot open a browser.
Push the grant once, then hydrate it at startup into a writable directory:

```bash
gmailscan-auth --push                    # once, from the machine that consented
```

```python
import os, tempfile
os.environ["GMAILSCAN_TOKEN_DIR"] = tempfile.mkdtemp()
from gmailscan.secrets import hydrate_tokens
hydrate_tokens()                         # raises loudly if the secret is empty
```

`hydrate_tokens` raises rather than returning nothing on an empty or
metadata-only secret: a silent zero-result sweep looks exactly like a mailbox
with no new mail.

## Tests

```bash
pip install -e ".[dev]"
ruff check src tests scripts && ruff format --check src tests scripts && mypy && pytest --cov
```

CI runs the same on every pull request, on Python 3.10 and 3.12, and fails the
build under 95% coverage. Three tiers, all offline, all run by default:

- **Unit** (`test_auth.py`, `test_client.py`, `test_secrets.py`, `test_cli.py`):
  the Gmail API, google-auth and boto3 faked one object at a time.
- **Component** (`pytest -m component`): `gmailscan-auth` run as a subprocess
  through its console-script entry point, checking exit status and output the
  way a cron line sees them.
- **End to end** (`pytest -m e2e`): the real token file, google-auth refresh,
  discovery client and httplib2 against a local fake Google in
  `tests/conftest.py`, a quota refusal with `Retry-After` and a revoked grant
  included. This is the tier that notices when google-auth changes.

## Releasing

1. Bump `version` in `pyproject.toml` and `__version__`, and write the
   CHANGELOG entry. The suite fails if the three disagree.
2. Tag and push: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`.

The Release workflow runs the suite on the tagged commit, checks the tag
against `__version__` and the CHANGELOG, builds a wheel and sdist, and
publishes the GitHub release with that version's CHANGELOG entry as its notes.
