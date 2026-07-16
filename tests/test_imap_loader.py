"""Tests for the read-only IMAP loader.

No network: a fake IMAP client records every method call, which lets the
tests assert the two read-only guarantees directly — the folder is selected
with ``readonly=True`` and bodies are fetched with ``BODY.PEEK[]`` — and that
no write-side command (STORE / EXPUNGE / APPEND) is ever issued.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.ingestion.imap_loader import (
    _imap_date,
    append_to_drafts,
    load_imap_unread,
)
from src.ingestion.mbox_loader import Email


def _raw_message(
    msg_id: str = "<m1@example.com>",
    subject: str = "Quarterly numbers",
    date: str = "Mon, 01 Jun 2026 10:00:00 +0000",
) -> bytes:
    return (
        f"Message-ID: {msg_id}\r\n"
        f"From: Alice <alice@example.com>\r\n"
        f"To: bob@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"\r\n"
        f"Here are the numbers.\r\n"
    ).encode()


class _FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL; records every call it sees."""

    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self.messages = messages  # uid -> raw RFC-5322 bytes
        self.calls: list[tuple[Any, ...]] = []
        self.logged_out = False

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", folder, readonly))
        return ("OK", [b"1"])

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, *args))
        if command == "search":
            uids = b" ".join(self.messages.keys())
            return ("OK", [uids])
        if command == "fetch":
            uid = args[0]
            return ("OK", [(b"1 (BODY[] {0}", self.messages[uid]), b")"])
        raise AssertionError(f"unexpected uid command: {command!r}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return ("BYE", [])

    def append(
        self, folder: str, flags: Any, date_time: Any, message: Any
    ) -> tuple[str, list[bytes]]:
        self.calls.append(("append", folder, flags, message))
        return ("OK", [b"[APPENDUID 1 1]"])


def test_selects_readonly_and_fetches_with_peek() -> None:
    fake = _FakeIMAP({b"1": _raw_message()})
    load_imap_unread(days=7, client=fake, folder="INBOX")

    select_calls = [c for c in fake.calls if c[0] == "select"]
    assert select_calls == [("select", "INBOX", True)]  # readonly=True

    fetch_calls = [c for c in fake.calls if c[0] == "uid" and c[1] == "fetch"]
    assert fetch_calls and all(c[3] == "(BODY.PEEK[])" for c in fetch_calls)


def test_search_criteria_is_unseen_since() -> None:
    fake = _FakeIMAP({b"1": _raw_message()})
    load_imap_unread(days=7, client=fake, folder="INBOX")
    search_calls = [c for c in fake.calls if c[0] == "uid" and c[1] == "search"]
    assert len(search_calls) == 1
    criteria = search_calls[0][2]
    assert criteria.startswith("(UNSEEN SINCE ") and criteria.endswith(")")


def test_never_issues_write_commands() -> None:
    fake = _FakeIMAP({b"1": _raw_message(), b"2": _raw_message("<m2@h>")})
    load_imap_unread(days=7, client=fake, folder="INBOX")
    commands = {c[1].lower() for c in fake.calls if c[0] == "uid"}
    assert commands <= {"search", "fetch"}  # no store / expunge / append
    method_names = {c[0] for c in fake.calls}
    assert method_names <= {"select", "uid"}


def test_returns_email_objects_matching_mbox_shape() -> None:
    fake = _FakeIMAP({b"1": _raw_message()})
    emails = load_imap_unread(days=7, client=fake, folder="INBOX")
    assert len(emails) == 1
    e = emails[0]
    assert isinstance(e, Email)
    assert e.id == "<m1@example.com>"
    assert e.from_addr == "alice@example.com"
    assert e.to_addrs == ["bob@example.com"]
    assert e.subject == "Quarterly numbers"
    assert "Here are the numbers." in e.body_plain
    assert e.date.year == 2026


def test_sorted_oldest_first_and_deduped() -> None:
    fake = _FakeIMAP(
        {
            b"1": _raw_message("<new@h>", date="Wed, 03 Jun 2026 10:00:00 +0000"),
            b"2": _raw_message("<old@h>", date="Mon, 01 Jun 2026 10:00:00 +0000"),
            b"3": _raw_message("<new@h>", date="Wed, 03 Jun 2026 10:00:00 +0000"),
        }
    )
    emails = load_imap_unread(days=7, client=fake, folder="INBOX")
    assert [e.id for e in emails] == ["<old@h>", "<new@h>"]


def test_injected_client_is_not_logged_out() -> None:
    # The caller owns an injected connection; we only log out our own.
    fake = _FakeIMAP({b"1": _raw_message()})
    load_imap_unread(days=7, client=fake, folder="INBOX")
    assert fake.logged_out is False


def test_missing_env_raises_naming_every_missing_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("IMAP_HOST", "IMAP_USER", "IMAP_PASS", "IMAP_FOLDER"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError) as excinfo:
        load_imap_unread(days=7)
    message = str(excinfo.value)
    assert "IMAP_HOST" in message
    assert "IMAP_USER" in message
    assert "IMAP_PASS" in message
    assert "app-specific password" in message


def test_imap_date_uses_english_months() -> None:
    assert _imap_date(datetime(2026, 6, 9, tzinfo=timezone.utc)) == "09-Jun-2026"
    assert _imap_date(datetime(2025, 12, 1, tzinfo=timezone.utc)) == "01-Dec-2025"


# --- append_to_drafts: the one and only write path -------------------------


def test_append_to_drafts_uses_draft_flag_and_folder() -> None:
    fake = _FakeIMAP({})
    append_to_drafts(b"raw message bytes", folder="[Gmail]/Drafts", client=fake)
    append_calls = [c for c in fake.calls if c[0] == "append"]
    assert len(append_calls) == 1
    _, folder, flags, message = append_calls[0]
    assert folder == "[Gmail]/Drafts"
    assert flags == r"(\Draft)"  # marks it a draft, never sends
    assert message == b"raw message bytes"


def test_append_to_drafts_defaults_folder_and_never_reads_or_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAP_DRAFTS_FOLDER", raising=False)
    fake = _FakeIMAP({})
    append_to_drafts(b"x", client=fake)
    methods = {c[0] for c in fake.calls}
    assert methods == {"append"}  # no select/uid/store/expunge, no send
    assert fake.calls[0][1] == "Drafts"  # DEFAULT_DRAFTS_FOLDER


def test_append_to_drafts_honors_imap_drafts_folder_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAP_DRAFTS_FOLDER", "Custom/Drafts")
    fake = _FakeIMAP({})
    append_to_drafts(b"x", client=fake)
    assert fake.calls[0][1] == "Custom/Drafts"


def test_append_to_drafts_raises_on_non_ok() -> None:
    class _RejectingIMAP(_FakeIMAP):
        def append(self, folder, flags, date_time, message):  # type: ignore[no-untyped-def]
            return ("NO", [b"[OVERQUOTA]"])

    with pytest.raises(RuntimeError) as excinfo:
        append_to_drafts(b"x", client=_RejectingIMAP({}))
    assert "APPEND" in str(excinfo.value)


def test_append_to_drafts_does_not_log_out_injected_client() -> None:
    fake = _FakeIMAP({})
    append_to_drafts(b"x", client=fake)
    assert fake.logged_out is False
