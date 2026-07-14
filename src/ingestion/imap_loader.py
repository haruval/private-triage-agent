"""Read-only IMAP ingestion: fetch unread messages as Email records.

Uses the standard library ``imaplib`` over SSL, configured entirely by
environment variables so credentials never appear on a command line:

    IMAP_HOST    server hostname, e.g. imap.gmail.com
    IMAP_USER    account login
    IMAP_PASS    app-specific password — never the main account password
    IMAP_FOLDER  mailbox to read (optional, default INBOX)

Read-only is enforced twice: the folder is selected with ``readonly=True``
(the server rejects any flag change for the whole session) and message
bodies are fetched with ``BODY.PEEK[]`` (which never sets ``\\Seen`` even on
a writable session). Nothing here can mark read, delete, or send — there is
no code path that issues STORE, EXPUNGE, or APPEND. Message bytes are parsed
with the same converter the mbox loader uses, so both sources yield
identical Email shapes.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from src.ingestion.mbox_loader import Email, email_from_message

logger = logging.getLogger(__name__)

DEFAULT_FOLDER = "INBOX"
DEFAULT_DAYS = 7

# IMAP SINCE dates use English month abbreviations regardless of locale;
# strftime("%b") follows LC_TIME, so spell them out.
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _imap_date(dt: datetime) -> str:
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def _config_from_env() -> tuple[str, str, str, str]:
    """Read connection settings, raising one error naming every missing var."""
    host = os.environ.get("IMAP_HOST", "").strip()
    user = os.environ.get("IMAP_USER", "").strip()
    password = os.environ.get("IMAP_PASS", "")
    folder = os.environ.get("IMAP_FOLDER", "").strip() or DEFAULT_FOLDER
    missing = [
        name
        for name, value in (
            ("IMAP_HOST", host),
            ("IMAP_USER", user),
            ("IMAP_PASS", password),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"Missing IMAP environment variable(s): {', '.join(missing)}. "
            f"Set IMAP_HOST, IMAP_USER, IMAP_PASS (and optionally IMAP_FOLDER). "
            f"Use an app-specific password, never the main account password."
        )
    return host, user, password, folder


def _message_bytes(fetch_data: list[Any]) -> bytes | None:
    """Pull the raw message bytes out of an imaplib FETCH response.

    imaplib returns a list mixing ``(envelope, payload)`` tuples and bare
    closing-paren bytes; the payload of the first tuple is the message.
    """
    for part in fetch_data:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
            return part[1]
    return None


def load_imap_unread(
    days: int = DEFAULT_DAYS,
    *,
    client: Any = None,
    folder: str | None = None,
) -> list[Email]:
    """Fetch unread messages from the last ``days`` days, oldest first.

    Pass an explicit ``client`` (any object exposing ``select`` / ``uid`` /
    ``logout``) for tests so no network or credentials are required;
    otherwise an ``imaplib.IMAP4_SSL`` connection is built from the
    environment and logged out when done. Messages that fail to fetch or
    parse are logged and skipped, matching the mbox loader's behavior.
    """
    own_client = client is None
    if own_client:
        host, user, password, env_folder = _config_from_env()
        folder = folder or env_folder
        client = imaplib.IMAP4_SSL(host)
        client.login(user, password)
    folder = folder or os.environ.get("IMAP_FOLDER", "").strip() or DEFAULT_FOLDER

    try:
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP select {folder!r} failed: {status}")

        since = _imap_date(datetime.now(timezone.utc) - timedelta(days=days))
        status, data = client.uid("search", f"(UNSEEN SINCE {since})")
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        uids = data[0].split() if data and data[0] else []

        emails: list[Email] = []
        seen_ids: set[str] = set()
        for uid in uids:
            status, fetch_data = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not fetch_data:
                logger.warning("IMAP fetch failed for uid %r; skipping", uid)
                continue
            raw = _message_bytes(fetch_data)
            if raw is None:
                logger.warning("IMAP fetch for uid %r had no payload; skipping", uid)
                continue
            try:
                email = email_from_message(email_lib.message_from_bytes(raw))
            except Exception:
                logger.exception("Failed to parse IMAP message uid %r; skipping", uid)
                continue
            if email.id in seen_ids:
                continue
            seen_ids.add(email.id)
            emails.append(email)

        emails.sort(key=lambda e: e.date)
        return emails
    finally:
        if own_client:
            try:
                client.logout()
            except Exception:
                logger.warning("IMAP logout failed", exc_info=True)
