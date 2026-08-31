"""``gmailscan-auth`` -- authorize a mailbox, and check the ones already granted.

Replaces two independently written ``setup_gmail_auth.py`` scripts that had
drifted in flags, defaults and documentation.

    gmailscan-auth --status                      # what is authorized, and healthy?
    gmailscan-auth --account you@gmail.com --manual --client-secret-file ~/client.json
    gmailscan-auth --push                        # copy local tokens into AWS

``--status`` is the one to reach for first. It refreshes nothing and grants
nothing; it reports which mailboxes have a token and whether that token still
works, which is the question behind almost every "why did the sweep find
nothing" investigation.

**On a headless box, use ``--manual``.** There is no local listener and no port
forward: the browser's redirect fails to load, but its address bar still carries
``?code=...``, and pasting that whole URL back completes the exchange. It must
go into *this* process -- the PKCE verifier lives on the flow object and is
never sent to Google, so a second run cannot finish the first run's consent.

The grant is ``gmail.readonly``. It cannot send, modify, label or delete mail.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from .auth import (
    SCOPES,
    SETUP_HINT,
    GmailAuthRequired,
    authorized_accounts,
    persist_token,
    token_dir,
    token_path,
)


def _status() -> int:
    """Report every authorized mailbox and whether its grant still works."""
    accounts = authorized_accounts()
    print(f"token directory: {token_dir()}")
    if not accounts:
        print("\nNo Gmail account is authorized here.")
        print(SETUP_HINT)
        return 1

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("\ngoogle-auth is not installed; cannot check token health.")
        for account in accounts:
            print(f"  {account:28} token present at {token_path(account)}")
        return 1

    print()
    failures = 0
    for account in accounts:
        path = token_path(account)
        try:
            creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call, unused-ignore]
                str(path), list(SCOPES)
            )
        except ValueError as exc:
            print(f"  {account:28} UNREADABLE  {exc}")
            failures += 1
            continue

        if creds.valid:
            print(f"  {account:28} OK          expires {creds.expiry}")
            continue
        try:
            creds.refresh(Request())
        except Exception as exc:
            detail = str(exc)
            print(f"  {account:28} DEAD        {detail[:80]}")
            if "invalid_grant" in detail:
                failures += 1
            continue
        persist_token(path, creds.to_json())
        print(f"  {account:28} REFRESHED   expires {creds.expiry}")

    if failures:
        print(f"\n{failures} account(s) need re-authorizing.")
        print(SETUP_HINT)
        return 1
    return 0


def _manual_consent(flow: object, port: int) -> object:
    """Consent without a local redirect server, for a headless box."""
    # The redirect is plain http on loopback; oauthlib refuses that by default.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    # Google may return a broader scope set than was requested. That is a
    # Google-side normalization, not a privilege escalation, and refusing it
    # only breaks the flow.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    flow.redirect_uri = f"http://localhost:{port}/"  # type: ignore[attr-defined]
    url, _ = flow.authorization_url(prompt="consent", access_type="offline")  # type: ignore[attr-defined]
    print("\nOpen this in a browser signed in as the account being authorized:\n")
    print(f"  {url}\n")
    print("The page it redirects to will fail to load. That is expected.")
    redirected = input("Paste the full localhost URL from the address bar: ").strip()
    flow.fetch_token(authorization_response=redirected)  # type: ignore[attr-defined]
    return flow.credentials  # type: ignore[attr-defined]


def _authorize(account: str, args: argparse.Namespace) -> int:
    path = token_path(account)
    if path.exists() and not args.force:
        print(f"{account} already has a token at {path}.")
        print("Check it with --status, or re-consent with --force.")
        return 0

    if not args.client_secret_file:
        print(
            "No token yet, so the OAuth client JSON is needed:\n"
            "  gmailscan-auth --account "
            f"{account} --client-secret-file ~/client.json\n\n"
            "One-time Google Cloud setup:\n"
            "  1. https://console.cloud.google.com -- create a project.\n"
            "  2. Enable the Gmail API.\n"
            "  3. OAuth consent screen: External, then PUBLISH THE APP TO PRODUCTION.\n"
            "     Left in Testing, Google expires refresh tokens after 7 days and the\n"
            "     grant dies a week later. Adding a test user does not prevent this.\n"
            "  4. Credentials > OAuth client ID > Desktop app. Download the JSON.",
            file=sys.stderr,
        )
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            'The consent flow needs the auth extra: pip install "gmailscan[auth]"',
            file=sys.stderr,
        )
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret_file, list(SCOPES))
    creds = _manual_consent(flow, args.port) if args.manual else flow.run_local_server(
        port=args.port, prompt="consent", access_type="offline"
    )

    if not getattr(creds, "refresh_token", None):
        print(
            "\nGoogle returned no refresh token, so this grant dies in an hour.\n"
            "Re-run with --force (the consent prompt only returns one on first grant).",
            file=sys.stderr,
        )
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    persist_token(path, creds.to_json())
    print(f"\nAuthorized {account}; token written to {path}")
    return 0


def _push(args: argparse.Namespace) -> int:
    try:
        from .secrets import push_tokens
    except ImportError:
        print('Needs the secrets extra: pip install "gmailscan[secrets]"', file=sys.stderr)
        return 2
    try:
        pushed = push_tokens(
            [args.account] if args.account else None,
            secret_name=args.secret_name,
            region=args.region,
            granted_at=datetime.now(timezone.utc).isoformat(),
        )
    except GmailAuthRequired as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Pushed to '{args.secret_name}': {', '.join(pushed)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gmailscan-auth", description="Authorize and inspect read-only Gmail grants."
    )
    parser.add_argument("--account", help="Gmail address to authorize")
    parser.add_argument(
        "--status", action="store_true", help="Report every authorized mailbox and its health"
    )
    parser.add_argument("--client-secret-file", help="OAuth client JSON from Google Cloud")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Headless consent: paste the redirect URL back instead of running a listener",
    )
    parser.add_argument("--port", type=int, default=8765, help="Loopback redirect port")
    parser.add_argument(
        "--force", action="store_true", help="Re-consent even if a token already exists"
    )
    parser.add_argument(
        "--push", action="store_true", help="Copy local tokens into AWS Secrets Manager"
    )
    parser.add_argument("--secret-name", default="gmail-tokens", help="Secrets Manager secret")
    parser.add_argument("--region", help="AWS region for --push")
    parser.add_argument("--json", action="store_true", help="Machine-readable --status output")
    args = parser.parse_args(argv)

    if args.push:
        return _push(args)
    if args.status or not args.account:
        if args.json:
            print(json.dumps({"token_dir": str(token_dir()), "accounts": authorized_accounts()}))
            return 0
        return _status()
    return _authorize(args.account.strip().lower(), args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
