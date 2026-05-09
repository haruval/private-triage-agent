"""Tests for src/ingestion/mbox_loader.py.

Each test generates its own tiny mbox fixture inside `tmp_path` rather than
relying on a shared file on disk — keeps tests independent and self-documenting.
"""

from __future__ import annotations

import textwrap
from datetime import datetime
from pathlib import Path

from src.ingestion.mbox_loader import Email, load_mbox


def _write_mbox(path: Path, messages: list[str]) -> None:
    """Write raw RFC-5322 messages into an mbox file at `path`.

    Each message is preceded by a `From ` separator line as the mbox format
    requires, and followed by a blank line.
    """
    with path.open("wb") as f:
        for msg in messages:
            f.write(b"From sender@example.com Mon Jan  1 00:00:00 2024\n")
            f.write(msg.encode("utf-8"))
            if not msg.endswith("\n"):
                f.write(b"\n")
            f.write(b"\n")


PLAIN_MSG = textwrap.dedent("""\
    Message-ID: <plain1@example.com>
    From: Alice <alice@example.com>
    To: Bob <bob@example.com>, carol@example.com
    Subject: Lunch tomorrow
    Date: Mon, 12 Jan 2024 10:30:00 -0500
    Content-Type: text/plain; charset=utf-8

    Hey Bob -- can you grab lunch at noon?
    Bring the deck draft.
    """)


def test_basic_email_fields_parse(tmp_path: Path) -> None:
    mbox_path = tmp_path / "single.mbox"
    _write_mbox(mbox_path, [PLAIN_MSG])

    emails = list(load_mbox(mbox_path))
    assert len(emails) == 1

    e = emails[0]
    assert isinstance(e, Email)
    assert e.id == "<plain1@example.com>"
    assert e.from_addr == "alice@example.com"
    assert e.to_addrs == ["bob@example.com", "carol@example.com"]
    assert e.subject == "Lunch tomorrow"
    assert isinstance(e.date, datetime)
    assert (e.date.year, e.date.month, e.date.day) == (2024, 1, 12)
    assert "lunch at noon" in e.body_plain
    assert e.thread_id is None
    # Headers preserved as a dict
    assert e.headers["Subject"] == "Lunch tomorrow"
    assert e.headers["Message-ID"] == "<plain1@example.com>"


MULTIPART_MSG = textwrap.dedent("""\
    Message-ID: <mp1@example.com>
    From: noreply@svc.example.com
    To: ari@example.com
    Subject: Your order
    Date: Tue, 13 Feb 2024 09:00:00 +0000
    MIME-Version: 1.0
    Content-Type: multipart/alternative; boundary="BNDRY"

    --BNDRY
    Content-Type: text/plain; charset=utf-8

    Order #1234 shipped.

    --BNDRY
    Content-Type: text/html; charset=utf-8

    <html><body><p>Order <b>#1234</b> shipped.</p></body></html>

    --BNDRY--
    """)


def test_multipart_prefers_plain_over_html(tmp_path: Path) -> None:
    mbox_path = tmp_path / "mp.mbox"
    _write_mbox(mbox_path, [MULTIPART_MSG])

    emails = list(load_mbox(mbox_path))
    assert len(emails) == 1
    body = emails[0].body_plain
    assert "Order #1234 shipped" in body
    # The HTML siblings shouldn't leak into the body when a plain part exists.
    assert "<b>" not in body
    assert "<html>" not in body


HTML_ONLY_MSG = textwrap.dedent("""\
    Message-ID: <html1@example.com>
    From: news@example.com
    To: ari@example.com
    Subject: Weekly digest
    Date: Wed, 14 Feb 2024 12:00:00 +0000
    MIME-Version: 1.0
    Content-Type: text/html; charset=utf-8

    <html><body>
    <h1>Hello</h1>
    <p>This week: <b>three</b> things to read.</p>
    <script>alert('nope');</script>
    </body></html>
    """)


def test_html_only_message_is_stripped_to_text(tmp_path: Path) -> None:
    mbox_path = tmp_path / "html.mbox"
    _write_mbox(mbox_path, [HTML_ONLY_MSG])

    emails = list(load_mbox(mbox_path))
    assert len(emails) == 1
    body = emails[0].body_plain
    assert "Hello" in body
    assert "three" in body
    assert "things to read" in body
    # Tags removed
    assert "<p>" not in body
    assert "<b>" not in body
    # <script> contents dropped, not just the tags
    assert "alert" not in body
    assert "nope" not in body


def test_deduplication_by_message_id(tmp_path: Path) -> None:
    """Identical Message-ID across two mbox entries yields one Email."""
    mbox_path = tmp_path / "dupes.mbox"
    _write_mbox(mbox_path, [PLAIN_MSG, PLAIN_MSG])

    emails = list(load_mbox(mbox_path))
    assert len(emails) == 1
    assert emails[0].id == "<plain1@example.com>"


REPLY_MSG = textwrap.dedent("""\
    Message-ID: <reply1@example.com>
    From: bob@example.com
    To: alice@example.com
    Subject: Re: Lunch tomorrow
    Date: Mon, 12 Jan 2024 11:00:00 -0500
    In-Reply-To: <plain1@example.com>
    References: <plain1@example.com>
    Content-Type: text/plain; charset=utf-8

    Sounds great, see you at noon.
    """)


def test_thread_id_picks_root_from_references(tmp_path: Path) -> None:
    mbox_path = tmp_path / "thread.mbox"
    _write_mbox(mbox_path, [REPLY_MSG])

    emails = list(load_mbox(mbox_path))
    assert len(emails) == 1
    assert emails[0].thread_id == "<plain1@example.com>"


NO_ID_MSG = textwrap.dedent("""\
    From: someone@example.com
    To: ari@example.com
    Subject: No message-id
    Date: Thu, 15 Feb 2024 09:00:00 +0000
    Content-Type: text/plain; charset=utf-8

    This message has no Message-ID header.
    """)


def test_missing_message_id_gets_synthesized_and_dedupes(tmp_path: Path) -> None:
    """When Message-ID is missing, fall back to a content hash that still dedupes."""
    mbox_path = tmp_path / "noid.mbox"
    _write_mbox(mbox_path, [NO_ID_MSG, NO_ID_MSG])

    emails = list(load_mbox(mbox_path))
    assert len(emails) == 1
    assert emails[0].id.startswith("<sha1:")
    assert emails[0].subject == "No message-id"
