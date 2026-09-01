"""gmailscan -- shared read-only Gmail access for the personal projects.

    from gmailscan import search_all, authorized_accounts

    # Every authorized mailbox, tagged with which one each hit came from.
    for msg in search_all("from:shipment-tracking@amazon.com", after=date(2026, 8, 1)):
        print(msg.account, msg.subject, msg.html_first[:80])

    # Or pin to one.
    for msg in search_all("subject:recruiter", accounts=["mdanifo@gmail.com"]):
        print(msg.text_first)

Before this, three projects each carried their own Gmail client -- three token
lookup orders, two of which reached into a fourth repo's working directory, and
two separately written consent scripts. This is the single canonical version.

The scope is ``gmail.readonly`` and nothing else: search and read, never send,
modify, label, or delete.

Auth is filesystem-based by default (``~/.config/google-oauth``). The optional
``gmailscan[secrets]`` extra adds an AWS Secrets Manager store for unattended
runs that cannot open a browser; ``boto3`` is imported lazily, so callers who
don't need it never pay for it.
"""

from __future__ import annotations

from .auth import (
    DEFAULT_TOKEN_DIR,
    SCOPES,
    SETUP_HINT,
    GmailAuthRequired,
    GmailUnavailable,
    authorized_accounts,
    is_configured,
    load_credentials,
    persist_token,
    token_dir,
    token_path,
)
from .client import (
    EmailMessage,
    GmailClient,
    clients,
    decode_message,
    dump_path,
    search_all,
)

__all__ = [
    "DEFAULT_TOKEN_DIR",
    "EmailMessage",
    "GmailAuthRequired",
    "GmailClient",
    "GmailUnavailable",
    "SCOPES",
    "SETUP_HINT",
    "authorized_accounts",
    "clients",
    "decode_message",
    "dump_path",
    "is_configured",
    "load_credentials",
    "persist_token",
    "search_all",
    "token_dir",
    "token_path",
]

__version__ = "0.1.5"
