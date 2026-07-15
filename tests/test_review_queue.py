"""Tests for the persistent review queue (src/review_queue.py).

Pure local file I/O against tmp_path — round-tripping records through the
append-only ledgers, skipping corrupt lines, and the pending-review ordering
that `review` relies on (importance desc, then oldest processed first).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.mbox_loader import Email
from src.review_queue import (
    ImapAccountRef,
    QueueRecord,
    append_records,
    append_reviewed,
    load_records,
    pending_records,
    processed_ids,
    processed_path,
    reviewed_ids,
)
from src.router.sensitivity_scorer import EscalationDecision
from src.triage.classifier import TriageResult


def _record(
    email_id: str = "<abc@host>",
    importance: float = 5.0,
    processed_at: str = "2026-06-09T10:00:00+00:00",
) -> QueueRecord:
    return QueueRecord(
        email=Email(
            id=email_id,
            from_addr="alice@example.com",
            to_addrs=["me@example.com"],
            subject="Hi",
            date=datetime(2026, 6, 1, tzinfo=timezone.utc),
            body_plain="contact alice@example.com please",
            thread_id=None,
            headers={"From": "alice@example.com"},
        ),
        result=TriageResult(
            category="needs_reply",
            confidence=0.9,
            summary="Alice wants a reply.",
            extracted_action_items=["reply to alice"],
            suggested_reply_draft="On it.",
            reasoning="r",
        ),
        decision=EscalationDecision(escalate=True, reason="because", score=0.6),
        draft="On it.",
        provenance="Claude",
        mapping={"Email_E1": "alice@example.com"},
        claude_used=True,
        error=None,
        importance=importance,
        importance_reason="time-sensitive request",
        ranked_by="Claude",
        source="mbox:enron_50.mbox",
        processed_at=processed_at,
    )


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    original = _record()
    original.imap_account = ImapAccountRef(
        host="imap.gmail.com",
        user="me@gmail.com",
        drafts_folder="[Gmail]/Drafts",
    )
    append_records(tmp_path, [original])
    loaded = load_records(tmp_path)
    assert len(loaded) == 1
    rec = loaded[0]
    assert rec.email == original.email
    assert rec.result == original.result
    assert rec.decision == original.decision
    assert rec.draft == original.draft
    assert rec.provenance == original.provenance
    assert rec.mapping == original.mapping
    assert rec.claude_used is True
    assert rec.error is None
    assert rec.importance == original.importance
    assert rec.importance_reason == original.importance_reason
    assert rec.ranked_by == original.ranked_by
    assert rec.source == original.source
    assert rec.processed_at == original.processed_at
    assert rec.imap_account == original.imap_account


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    append_records(tmp_path, [_record()])
    with processed_path(tmp_path).open("a") as f:
        f.write("not json\n")
        f.write('{"email": "not a dict"}\n')
    assert len(load_records(tmp_path)) == 1


def test_processed_ids(tmp_path: Path) -> None:
    append_records(tmp_path, [_record("<a@h>"), _record("<b@h>")])
    assert processed_ids(tmp_path) == {"<a@h>", "<b@h>"}


def test_empty_queue_dir(tmp_path: Path) -> None:
    assert load_records(tmp_path) == []
    assert processed_ids(tmp_path) == set()
    assert reviewed_ids(tmp_path) == set()
    assert pending_records(tmp_path) == []


def test_reviewed_ledger_and_pending(tmp_path: Path) -> None:
    append_records(tmp_path, [_record("<a@h>"), _record("<b@h>")])
    append_reviewed(tmp_path, "<a@h>", "approve", Path("data/approved_drafts/x.txt"))
    assert reviewed_ids(tmp_path) == {"<a@h>"}
    pending = pending_records(tmp_path)
    assert [r.email.id for r in pending] == ["<b@h>"]


def test_pending_sorted_by_importance_then_age(tmp_path: Path) -> None:
    append_records(
        tmp_path,
        [
            _record("<low@h>", importance=2.0),
            _record("<hi@h>", importance=9.0),
            _record(
                "<mid-new@h>", importance=5.0, processed_at="2026-06-09T12:00:00+00:00"
            ),
            _record(
                "<mid-old@h>", importance=5.0, processed_at="2026-06-09T08:00:00+00:00"
            ),
        ],
    )
    pending = pending_records(tmp_path)
    assert [r.email.id for r in pending] == [
        "<hi@h>",
        "<mid-old@h>",
        "<mid-new@h>",
        "<low@h>",
    ]


def test_pending_dedupes_by_email_id_keeping_first(tmp_path: Path) -> None:
    append_records(tmp_path, [_record("<a@h>", importance=3.0)])
    append_records(tmp_path, [_record("<a@h>", importance=8.0)])
    pending = pending_records(tmp_path)
    assert len(pending) == 1
    assert pending[0].importance == 3.0
