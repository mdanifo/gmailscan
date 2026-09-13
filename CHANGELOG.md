# Changelog

One entry per release, saying what it fixes and what it breaks, because that is
the whole question when deciding whether a bump can wait. Until this file
existed the answer lived in `git log`, which nobody reads before a bump they do
not know they need: jobpipe sat on v0.1.3 through five releases, including the
one that fixed its own four-day Gmail outage.

Pin a tag: `gmailscan @ git+https://github.com/mdanifo/gmailscan@v0.3.0`.

## v0.3.0 (2026-09-13)

**Take this if you call `get_thread()` or `raw()`.**

Fixes

- `get_thread()` and `raw()` wait out Gmail's per-minute quota the way
  `search()` has since v0.2.0. On v0.2.x one quota refusal on a thread read
  raised at once: the 2026-09-13 outreach sweep lost about six thread reads that
  way while its searches rode straight through.

Adds

- `health(account)` and `GmailClient.health()`: refresh the grant and report
  `missing`, `configured`, `expired`, `revoked` or `ok`. `is_configured()` still
  only checks that the token file exists, and still answers `True` for a
  revoked grant.
- One INFO line per client built: `gmailscan 0.3.0 reading <address>`.
- README: the quota model (6,000 units per mailbox per minute, what each call
  costs, how to size `limit` and `after`), and a test consumers can copy to
  catch an environment running something other than its pin. Also that the CLI
  wants both extras, `gmailscan[auth,secrets]`.

Breaks

- Nothing.

## v0.2.1 (2026-09-05)

Fixes

- The backoff could not outlast the quota window. It gave up after about 26
  seconds against a limit Gmail meters per minute, so a long sweep died just
  before the window reset. Now nine attempts, each wait capped at 90 seconds,
  and `Retry-After` honoured (clamped to 120 seconds).

Breaks

- Nothing, though a sweep that hits the quota now sleeps for up to several
  minutes rather than raising. A caller with its own timeout should allow for
  that.

## v0.2.0 (2026-09-05)

**The release that fixed jobpipe's four-day outage.** Take v0.2.1 instead:
this one's backoff gives up before the quota window resets.

Fixes

- `search()` retries rate limits and transient server errors (429, 5xx, and the
  403 that says `rateLimitExceeded`) instead of raising on the first. Before
  this, one refusal ended the sweep. A real 403, meaning a revoked grant or the
  wrong scope, still raises at once.

Adds

- `search(headers_only=True)` and `search_all(headers_only=True)`: fetch just
  From, To, Subject and Date. `text` and `html` come back `None`, so it is
  opt-in.

Breaks

- Nothing.

## v0.1.5 (2026-08-31)

Fixes

- `__version__` and `pyproject.toml` match the tag. Every earlier tag reports
  `0.1.0`, so a version check against any of them means nothing.

Breaks

- Nothing.

## v0.1.4 (2026-08-31)

Fixes

- `gmailscan-auth --status` shows how old each grant is, from a `.granted`
  sidecar written at consent, instead of the access token's expiry. That
  expiry is about an hour out and renews itself; it read as a deadline and hid
  the number that matters. Grants made before this show "granted date unknown"
  until the account is re-consented.

Breaks

- The `--status` output changed: `expires <timestamp>` became
  `granted Nd ago (<date>)`. Anything parsing it needs updating.

## v0.1.3 (2026-08-31)

Fixes

- Only `id`, `subject`, `sender` and `date` are required to build an
  `EmailMessage`. `threadId`, `text` and `html` got defaults, so a hand-built
  fixture for mail that never threads no longer has to invent them.

Breaks

- **Positional construction.** `threadId` moved from second place to after
  `html`. `EmailMessage("id", "thread", "subject", ...)` now puts the thread id
  in `subject` without an error. Keyword arguments are unaffected.

## v0.1.2 (2026-08-31)

Adds

- `GmailClient.token_file()`, a hook for a consumer that keeps its own
  pre-gmailscan token setting, and `load_credentials(path=...)`. Discovery,
  `is_configured` and credential loading then cannot disagree about which file
  is authoritative.

Breaks

- Nothing.

## v0.1.1 (2026-08-31)

Fixes

- `search_all()` skips a mailbox whose grant has died and reads the rest,
  logging a warning. Before, the first dead mailbox aborted the sweep and hid
  the healthy one's mail.

Breaks

- A single dead mailbox no longer raises. A caller that relied on
  `GmailAuthRequired` to notice one needs `health()` (v0.3.0) or the log
  warning. It still raises when every mailbox fails.

## v0.1.0 (2026-08-31)

- One read-only Gmail client in place of the three that `amazon-ledger`,
  `open-job-aggregator` and `virtual-closet` each carried. One token store,
  `~/.config/google-oauth/`, instead of three lookup orders, two of which read
  from a sibling repo's working directory.
