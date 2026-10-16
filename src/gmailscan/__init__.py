"""gmailscan: read-only Gmail access shared by the personal projects.

    from gmailscan import search_all, authorized_accounts

    # Every authorized mailbox, each hit tagged with the one it came from.
    for msg in search_all("from:shipment-tracking@amazon.com", after=date(2026, 8, 1)):
        print(msg.account, msg.subject, msg.html_first[:80])

    # Or pin to one.
    for msg in search_all("subject:recruiter", accounts=["you@gmail.com"]):
        print(msg.text_first)

The scope is ``gmail.readonly``: search and read, no sending, labelling or
deleting.

Tokens live in ``~/.config/google-oauth`` by default. The ``gmailscan[secrets]``
extra adds an AWS Secrets Manager store for unattended runs; ``boto3`` is
imported only when that store is used.
"""

from __future__ import annotations

from .auth import (
    DEFAULT_TOKEN_DIR,
    SCOPES,
    SETUP_HINT,
    GmailAuthRequired,
    GmailUnavailable,
    Health,
    authorized_accounts,
    health,
    health_detail,
    is_configured,
    load_credentials,
    persist_token,
    token_dir,
    token_path,
)
from .client import (
    EmailMessage,
    GmailClient,
    MessageRef,
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
    "Health",
    "MessageRef",
    "SCOPES",
    "SETUP_HINT",
    "authorized_accounts",
    "clients",
    "decode_message",
    "dump_path",
    "health",
    "health_detail",
    "is_configured",
    "load_credentials",
    "persist_token",
    "search_all",
    "token_dir",
    "token_path",
]

__version__ = "0.5.0"
