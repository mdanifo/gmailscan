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
```

`boto3` is only needed for the AWS token store and `google-auth-oauthlib` only
for the consent flow — a project that just reads mail needs neither.

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

  mdanifo100@gmail.com         DEAD        invalid_grant
  mdanifo@gmail.com            REFRESHED   expires 2026-08-31 13:01:12
```

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
pytest
```

The Gmail API is faked throughout — the suite never touches the network or a
real token.
