"""Tests for the persistent review queue (src/review_queue.py).

Pure local file I/O against tmp_path — round-tripping records through the
append-only ledgers, skipping corrupt lines, and the pending-review ordering
that `review` relies on (importance desc, then oldest processed first).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.mbox_loader import Email
from src.review_queue import (
    ImapAccountRef,
    QueueRecord,
    append_records,
    append_reviewed,
    compute_record_id,
    load_records,
    pending_records,
    processed_path,
    processed_record_ids,
    reviewed_ids,
    reviewed_keys,
)
from src.router.sensitivity_scorer import EscalationDecision
from src.triage.classifier import TriageResult


def _record(
    email_id: str = "<abc@host>",
    importance: float = 5.0,
    processed_at: str = "2026-06-09T10:00:00+00:00",
    source: str = "mbox:enron_50.mbox",
    imap_account: ImapAccountRef | None = None,
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
        source=source,
        processed_at=processed_at,
        imap_account=imap_account,
    )


def _account(user: str = "me@gmail.com") -> ImapAccountRef:
    return ImapAccountRef(
        host="imap.gmail.com", user=user, drafts_folder="[Gmail]/Drafts"
    )


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    original = _record(imap_account=_account())
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
    assert rec.record_id == original.record_id


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    append_records(tmp_path, [_record()])
    with processed_path(tmp_path).open("a") as f:
        f.write("not json\n")
        f.write('{"email": "not a dict"}\n')
    assert len(load_records(tmp_path)) == 1


def test_processed_record_ids(tmp_path: Path) -> None:
    records = [_record("<a@h>"), _record("<b@h>")]
    append_records(tmp_path, records)
    assert processed_record_ids(tmp_path) == {r.record_id for r in records}
    # mbox record ids depend only on the Message-ID, not the mbox filename.
    assert processed_record_ids(tmp_path) == {
        compute_record_id("mbox", None, "<a@h>"),
        compute_record_id("mbox", None, "<b@h>"),
    }


def test_empty_queue_dir(tmp_path: Path) -> None:
    assert load_records(tmp_path) == []
    assert processed_record_ids(tmp_path) == set()
    assert reviewed_ids(tmp_path) == set()
    assert pending_records(tmp_path) == []


def test_reviewed_ledger_and_pending(tmp_path: Path) -> None:
    rec_a, rec_b = _record("<a@h>"), _record("<b@h>")
    append_records(tmp_path, [rec_a, rec_b])
    append_reviewed(
        tmp_path,
        rec_a.record_id,
        "<a@h>",
        "approve",
        Path("data/approved_drafts/x.txt"),
    )
    assert reviewed_ids(tmp_path) == {"<a@h>"}
    assert reviewed_keys(tmp_path).record_ids == {rec_a.record_id}
    assert reviewed_keys(tmp_path).legacy_email_ids == set()
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


def test_pending_dedupes_by_record_id_keeping_first(tmp_path: Path) -> None:
    append_records(tmp_path, [_record("<a@h>", importance=3.0)])
    append_records(tmp_path, [_record("<a@h>", importance=8.0)])
    pending = pending_records(tmp_path)
    assert len(pending) == 1
    assert pending[0].importance == 3.0


# ---------------------------------------------------------------------------
# Record identity: account scoping + legacy-ledger compatibility
# ---------------------------------------------------------------------------


def test_same_message_id_in_two_accounts_is_two_records(tmp_path: Path) -> None:
    a = _record("<same@h>", source="imap", imap_account=_account("one@gmail.com"))
    b = _record("<same@h>", source="imap", imap_account=_account("two@gmail.com"))
    assert a.record_id != b.record_id
    append_records(tmp_path, [a, b])
    assert len(pending_records(tmp_path)) == 2

    # Reviewing one account's copy leaves the other pending.
    append_reviewed(tmp_path, a.record_id, "<same@h>", "approve", None)
    pending = pending_records(tmp_path)
    assert [r.record_id for r in pending] == [b.record_id]


def test_mbox_record_ids_ignore_the_source_filename() -> None:
    a = _record("<same@h>", source="mbox:one.mbox")
    b = _record("<same@h>", source="mbox:two.mbox")
    assert a.record_id == b.record_id  # cross-file dedupe preserved


def test_legacy_processed_line_derives_a_record_id(tmp_path: Path) -> None:
    """Ledger lines written before record_id existed still load and dedupe."""
    record = _record("<old@h>")
    line = record.to_json_dict()
    del line["record_id"]
    path = processed_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(line) + "\n")

    (loaded,) = load_records(tmp_path)
    assert loaded.record_id == compute_record_id("mbox", None, "<old@h>")
    assert processed_record_ids(tmp_path) == {loaded.record_id}


def test_legacy_reviewed_entry_suppresses_unscoped_records_only(
    tmp_path: Path,
) -> None:
    """An old reviewed line (raw email_id) hides mbox/legacy-imap records with
    that Message-ID, but never a newly ingested account-scoped record."""
    mbox_rec = _record("<same@h>", source="mbox:x.mbox")
    legacy_imap_rec = _record("<same@h>", source="imap", imap_account=None)
    scoped_rec = _record("<same@h>", source="imap", imap_account=_account())
    append_records(tmp_path, [mbox_rec, legacy_imap_rec, scoped_rec])

    # A pre-record_id reviewed entry: email_id only.
    reviewed = tmp_path / "reviewed.jsonl"
    reviewed.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "email_id": "<same@h>",
                "action": "approve",
                "approved_path": None,
            }
        )
        + "\n"
    )

    pending = pending_records(tmp_path)
    assert [r.record_id for r in pending] == [scoped_rec.record_id]
