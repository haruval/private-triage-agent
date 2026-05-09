"""Load emails from an .mbox file as a stream of typed Email records.

Real-world mbox files are messy: missing headers, RFC 2047 encoded subjects,
multipart/alternative with HTML-only bodies, duplicate messages from
forwards-of-forwards. The loader yields one Email per unique Message-ID and
falls back to a content hash when Message-ID is absent.
"""

from __future__ import annotations

import hashlib
import logging
import mailbox
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


@dataclass
class Email:
    id: str
    from_addr: str
    to_addrs: list[str]
    subject: str
    date: datetime
    body_plain: str
    thread_id: str | None
    headers: dict[str, str]


# ---------------------------------------------------------------------------
# HTML stripping
# ---------------------------------------------------------------------------


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML to text. Drops <script> and <style> contents.

    Inserts newlines around block-ish tags so the output preserves paragraph
    structure rather than collapsing to one line.
    """

    _BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4"}
    _DROP_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._DROP_TAGS:
            self._drop_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP_TAGS and self._drop_depth > 0:
            self._drop_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        text = "".join(self._chunks)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # HTMLParser is forgiving but not infallible; defend anyway.
        return html
    return parser.get_text()


# ---------------------------------------------------------------------------
# Header / payload helpers
# ---------------------------------------------------------------------------


def _decode_header_value(value: str | None) -> str:
    """Decode RFC 2047 encoded headers (=?utf-8?B?...?=) to plain str."""
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: Message) -> str:
    """Pull a plain-text body out of a possibly-multipart message.

    Strategy: collect all text/plain parts (skipping attachments). If none,
    fall back to text/html parts stripped to text. Otherwise empty string.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain":
                plain_parts.append(_decode_payload(part))
            elif ctype == "text/html":
                html_parts.append(_decode_payload(part))
    else:
        ctype = msg.get_content_type()
        if ctype == "text/html":
            html_parts.append(_decode_payload(msg))
        else:
            plain_parts.append(_decode_payload(msg))

    if any(p.strip() for p in plain_parts):
        return "\n".join(p for p in plain_parts if p).strip()
    if html_parts:
        return _html_to_text("\n".join(html_parts)).strip()
    return ""


def _parse_addr_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [addr for _, addr in getaddresses([value]) if addr]


def _parse_date(value: str | None) -> datetime:
    if not value:
        return EPOCH
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return EPOCH


def _thread_id(msg: Message) -> str | None:
    """Pick the root Message-ID of the thread.

    Prefers the first entry in `References` (oldest ancestor), falls back to
    `In-Reply-To`. Returns None for a root message with no parent.
    """
    references = msg.get("References")
    if references:
        ids = re.findall(r"<[^<>]+>", references)
        if ids:
            return ids[0]
    in_reply_to = msg.get("In-Reply-To")
    if in_reply_to:
        match = re.search(r"<[^<>]+>", in_reply_to)
        if match:
            return match.group(0)
    return None


def _stable_id(msg: Message) -> str:
    """Synthesize a content-addressable id when Message-ID is missing.

    Two messages without Message-ID that share the same key headers + first
    1KB of body collide on purpose — that's the dedup we want.
    """
    h = hashlib.sha1()
    for name in ("From", "To", "Subject", "Date"):
        h.update((msg.get(name, "") or "").encode("utf-8", errors="replace"))
        h.update(b"\x00")
    if not msg.is_multipart():
        body = msg.get_payload()
        if isinstance(body, str):
            h.update(body[:1024].encode("utf-8", errors="replace"))
    return f"<sha1:{h.hexdigest()}@local>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_mbox(path: str | Path) -> Iterator[Email]:
    """Yield Email records from an mbox file, deduped by Message-ID.

    Malformed messages are logged and skipped rather than raising — the
    triage pipeline should keep going on bad data.
    """
    seen_ids: set[str] = set()
    box = mailbox.mbox(str(path))
    try:
        for raw in box:
            try:
                msg_id = (raw.get("Message-ID") or "").strip()
                if not msg_id:
                    msg_id = _stable_id(raw)
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)

                _, from_addr = parseaddr(raw.get("From") or "")
                yield Email(
                    id=msg_id,
                    from_addr=from_addr,
                    to_addrs=_parse_addr_list(raw.get("To")),
                    subject=_decode_header_value(raw.get("Subject")),
                    date=_parse_date(raw.get("Date")),
                    body_plain=_extract_body(raw),
                    thread_id=_thread_id(raw),
                    headers={k: _decode_header_value(v) for k, v in raw.items()},
                )
            except Exception:
                logger.exception("Failed to parse message; skipping")
                continue
    finally:
        box.close()
