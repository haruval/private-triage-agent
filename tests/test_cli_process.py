"""Tests for the `process` command machinery in src/cli.py.

The pipeline glue is exercised with injected fakes — no Ollama, no spaCy, no
Claude network call. (Project policy is to use the real Claude API for the
delegate/eval tests; here we're testing the CLI's orchestration, fallback, and
file-writing logic, which is pure local code.) The escalation path is verified
end-to-end with a fake anonymizer + fake Claude so the anonymize → delegate →
rehydrate round-trip is checked without a model.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.cli import _build_processed, _email_payload, _process_worker
from src.ingestion.mbox_loader import Email
from src.review_actions import (
    ImapAccountMismatchError,
    ProcessedEmail,
    build_reply_message,
    persist_approved,
    reply_subject,
    safe_stem,
    save_approved_draft,
    session_record,
    source_is_imap,
)
from src.review_queue import ImapAccountRef
from src.router.sensitivity_scorer import EscalationDecision
from src.triage.classifier import TriageResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAnonymizer:
    """Maps a fixed token to a placeholder, like the real anonymizers' surface."""

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        out = text.replace("alice@example.com", "Email_E1")
        return out, {"Email_E1": "alice@example.com"}


class _EchoClaude:
    """Returns a draft that echoes the placeholder, as the real prompt requires."""

    model = "fake-claude"

    def delegate(self, anonymized_email: str, anonymized_thread: Any, task: str) -> str:
        assert (
            "Email_E1" in anonymized_email
        )  # the PII was anonymized before delegation
        assert "alice@example.com" not in anonymized_email
        return "Sure — I'll follow up with Email_E1 today."


class _BoomClaude:
    model = "fake-claude"

    def delegate(self, *a: Any, **k: Any) -> str:
        raise RuntimeError("api exploded")


class _FakeScorer:
    """Escalates exactly the email ids it was given."""

    def __init__(self, escalate_ids: set[str] | None = None) -> None:
        self.escalate_ids = escalate_ids or set()

    def score(self, email: Email, result: TriageResult) -> EscalationDecision:
        return _decision(escalate=email.id in self.escalate_ids)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _email(
    body: str = "ping", subject: str = "Hi", email_id: str = "<abc@host>"
) -> Email:
    return Email(
        id=email_id,
        from_addr="alice@example.com",
        to_addrs=["me@example.com"],
        subject=subject,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        body_plain=body,
        thread_id=None,
        headers={},
    )


def _result(
    draft: str | None = "local draft", category: str = "needs_reply"
) -> TriageResult:
    return TriageResult(
        category=category,
        confidence=0.9,
        summary="s",
        extracted_action_items=["do x"],
        suggested_reply_draft=draft,
        reasoning="r",
    )


def _decision(escalate: bool) -> EscalationDecision:
    return EscalationDecision(
        escalate=escalate, reason="because", score=0.6 if escalate else 0.1
    )


# ---------------------------------------------------------------------------
# _email_payload
# ---------------------------------------------------------------------------


def test_email_payload_has_subject_from_body_but_no_task() -> None:
    payload = _email_payload(_email(body="the body", subject="Subj"))
    assert "Subject: Subj" in payload
    assert "From: alice@example.com" in payload
    assert "the body" in payload
    assert "Task:" not in payload  # the Claude client adds the task, not us


# ---------------------------------------------------------------------------
# _build_processed
# ---------------------------------------------------------------------------


def test_not_escalated_uses_local_draft() -> None:
    p = _build_processed(
        _email(),
        _result(draft="local draft"),
        _decision(escalate=False),
        anonymizer=_FakeAnonymizer(),
        claude_client=_EchoClaude(),  # present, but must not be used
        task="reply",
    )
    assert p.provenance == "local"
    assert p.draft == "local draft"
    assert p.claude_used is False
    assert p.mapping == {}
    assert p.error is None


def test_escalated_delegates_and_rehydrates() -> None:
    p = _build_processed(
        _email(body="contact alice@example.com please"),
        _result(draft="local draft"),
        _decision(escalate=True),
        anonymizer=_FakeAnonymizer(),
        claude_client=_EchoClaude(),
        task="reply",
    )
    assert p.claude_used is True
    assert p.provenance == "Claude"
    # The placeholder Claude echoed was rehydrated back to the real value.
    assert "alice@example.com" in p.draft
    assert "Email_E1" not in p.draft
    assert p.mapping == {"Email_E1": "alice@example.com"}
    assert p.error is None


def test_escalated_without_client_falls_back_to_local() -> None:
    p = _build_processed(
        _email(),
        _result(draft="local draft"),
        _decision(escalate=True),
        anonymizer=_FakeAnonymizer(),
        claude_client=None,
        task="reply",
    )
    assert p.provenance == "local"
    assert p.draft == "local draft"
    assert p.claude_used is False
    assert p.error and "no Claude client" in p.error


def test_escalated_delegate_failure_falls_back_to_local() -> None:
    p = _build_processed(
        _email(),
        _result(draft="local draft"),
        _decision(escalate=True),
        anonymizer=_FakeAnonymizer(),
        claude_client=_BoomClaude(),
        task="reply",
    )
    assert p.provenance == "local"
    assert p.draft == "local draft"
    assert p.claude_used is False
    assert p.error and "delegation failed" in p.error
    assert "RuntimeError" in p.error


# ---------------------------------------------------------------------------
# Saving approved drafts
# ---------------------------------------------------------------------------


def _processed(draft: str, email_id: str = "<abc@host>") -> ProcessedEmail:
    return ProcessedEmail(
        email=_email(email_id=email_id),
        result=_result(),
        decision=_decision(escalate=False),
        draft=draft,
        provenance="local",
        mapping={},
        claude_used=False,
        error=None,
    )


def test_safe_stem_sanitizes_message_id() -> None:
    stem = safe_stem(_email(email_id="<a/b c@host>"))
    assert "/" not in stem and " " not in stem and "<" not in stem
    assert stem  # non-empty


def test_save_approved_draft_writes_header_and_body(tmp_path: Path) -> None:
    out = tmp_path / "approved"
    path = save_approved_draft(_processed("Hello there."), "Hello there.", out)
    assert path.exists()
    assert path.parent == out
    content = path.read_text()
    assert "Subject: Re: Hi" in content
    assert "X-Draft-Provenance: local" in content
    assert content.rstrip().endswith("Hello there.")


def test_save_approved_draft_does_not_clobber(tmp_path: Path) -> None:
    out = tmp_path / "approved"
    p1 = save_approved_draft(_processed("one"), "one", out)
    p2 = save_approved_draft(_processed("two"), "two", out)
    assert p1 != p2
    assert p1.exists() and p2.exists()
    assert "one" in p1.read_text()
    assert "two" in p2.read_text()


# ---------------------------------------------------------------------------
# Reply message building + .eml export
# ---------------------------------------------------------------------------


def test_reply_subject_prefixes_once() -> None:
    assert reply_subject("Lunch") == "Re: Lunch"
    assert reply_subject("Re: Lunch") == "Re: Lunch"  # no doubling
    assert reply_subject("RE: Lunch") == "RE: Lunch"  # case-insensitive
    assert reply_subject("") == "Re: (no subject)"


def test_build_reply_message_headers_and_body() -> None:
    p = _processed("the reply", email_id="<orig@host>")
    msg = build_reply_message(p, "the reply", sender="me@example.com")
    assert msg["To"] == "alice@example.com"  # reply goes to the sender
    assert msg["From"] == "me@example.com"  # the explicitly passed account
    assert msg["Subject"] == "Re: Hi"
    assert msg["In-Reply-To"] == "<orig@host>"  # threads to the original
    assert msg["References"] == "<orig@host>"
    assert msg["X-Draft-Provenance"] == "local"
    assert msg.get_content().strip() == "the reply"


def test_build_reply_message_ignores_global_imap_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sender passed -> no From, even with an unrelated IMAP account
    configured — the mail client must pick the sending account for mbox
    replies, not whatever IMAP connection happens to be saved."""
    monkeypatch.setenv("IMAP_USER", "unrelated-account@gmail.com")
    msg = build_reply_message(_processed("body"), "body")
    assert "From" not in msg
    assert "unrelated-account@gmail.com" not in msg.as_string()


def test_build_reply_message_skips_threading_for_synthetic_id() -> None:
    p = _processed("body", email_id="<sha1:deadbeef@local>")
    msg = build_reply_message(p, "body")
    assert "In-Reply-To" not in msg  # a content-hash id is not a real Message-ID
    assert "References" not in msg
    assert "From" not in msg  # no sender passed -> client fills it in


# ---------------------------------------------------------------------------
# persist_approved routing (mbox -> .eml, imap -> IMAP Drafts)
# ---------------------------------------------------------------------------


def testsource_is_imap() -> None:
    assert source_is_imap("imap")
    assert source_is_imap("imap:INBOX")
    assert not source_is_imap("mbox:enron_50.mbox")
    assert not source_is_imap("")  # unknown/legacy -> treat as mbox


def _imap_env(monkeypatch: pytest.MonkeyPatch, account: ImapAccountRef) -> None:
    monkeypatch.setenv("IMAP_HOST", account.host)
    monkeypatch.setenv("IMAP_USER", account.user)


def _gmail_account(user: str = "me@gmail.com") -> ImapAccountRef:
    return ImapAccountRef(
        host="imap.gmail.com", user=user, drafts_folder="[Gmail]/Drafts"
    )


def test_persist_mbox_source_writes_eml_no_imap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bytes] = []
    monkeypatch.setattr(
        "src.review_actions.append_to_drafts", lambda raw: calls.append(raw)
    )
    out = tmp_path / "approved"
    txt = persist_approved(_processed("hi"), "hi", out, "mbox:enron_50.mbox").txt_path
    assert txt.suffix == ".txt" and txt.exists()
    assert list(out.glob("*.eml"))  # .eml emitted for mbox
    assert calls == []  # never touches IMAP


def test_persist_mbox_eml_has_no_from_with_unrelated_imap_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An mbox approval while some other IMAP account is configured must not
    stamp that account into the .eml's From header."""
    _imap_env(monkeypatch, _gmail_account("unrelated@gmail.com"))
    out = tmp_path / "approved"
    persist_approved(_processed("hi"), "hi", out, "mbox:enron_50.mbox")
    eml = next(out.glob("*.eml")).read_bytes().decode()
    assert "unrelated@gmail.com" not in eml
    assert not eml.startswith("From:") and "\nFrom:" not in eml


def test_persist_imap_source_appends_draft_no_eml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _gmail_account()
    _imap_env(monkeypatch, account)
    calls: list[bytes] = []

    def _append(raw: bytes, *, folder: str | None = None) -> None:
        calls.append(raw)

    monkeypatch.setattr("src.review_actions.append_to_drafts", _append)
    out = tmp_path / "approved"
    txt = persist_approved(_processed("hi"), "hi", out, "imap:INBOX", account).txt_path
    assert txt.suffix == ".txt" and txt.exists()
    assert not list(out.glob("*.eml"))  # no .eml for imap
    assert len(calls) == 1  # appended to Drafts once
    # The draft carries the stored account as From, not a global env read.
    assert b"From: me@gmail.com" in calls[0]


def test_persist_imap_append_failure_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = _gmail_account()
    _imap_env(monkeypatch, account)

    def _boom(raw: bytes, *, folder: str | None = None) -> None:
        raise RuntimeError("IMAP APPEND to 'Drafts' failed")

    monkeypatch.setattr("src.review_actions.append_to_drafts", _boom)
    out = tmp_path / "approved"
    # The .txt still lands even though the Drafts APPEND raised.
    txt = persist_approved(_processed("hi"), "hi", out, "imap", account).txt_path
    assert txt.exists() and "hi" in txt.read_text()


def test_persist_imap_without_account_metadata_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy queue records (imap source, no stored account) must not append a
    draft into whatever account is configured now — the user is told to reject
    and reprocess instead."""
    _imap_env(monkeypatch, _gmail_account())
    calls: list[bytes] = []
    monkeypatch.setattr(
        "src.review_actions.append_to_drafts", lambda raw, **kw: calls.append(raw)
    )
    with pytest.raises(ImapAccountMismatchError, match="reprocess"):
        persist_approved(_processed("hi"), "hi", tmp_path, "imap", None)
    assert calls == []
    assert not list(tmp_path.glob("*.txt"))  # refused before anything was written


def test_persist_imap_uses_ingested_account_and_drafts_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("IMAP_USER", "me@gmail.com")
    calls: list[tuple[bytes, str | None]] = []

    def _append(raw: bytes, *, folder: str | None = None) -> None:
        calls.append((raw, folder))

    monkeypatch.setattr("src.review_actions.append_to_drafts", _append)
    account = ImapAccountRef(
        host="imap.gmail.com",
        user="me@gmail.com",
        drafts_folder="[Gmail]/Drafts",
    )
    persist_approved(_processed("hi"), "hi", tmp_path, "imap", account)
    assert len(calls) == 1
    assert calls[0][1] == "[Gmail]/Drafts"


def test_persist_imap_blocks_a_different_active_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("IMAP_USER", "other@gmail.com")
    account = ImapAccountRef(
        host="imap.gmail.com",
        user="original@gmail.com",
        drafts_folder="[Gmail]/Drafts",
    )
    with pytest.raises(ImapAccountMismatchError):
        persist_approved(_processed("hi"), "hi", tmp_path, "imap", account)
    assert not list(tmp_path.glob("*.txt"))


def test_persist_eml_and_txt_share_stem(tmp_path: Path) -> None:
    out = tmp_path / "approved"
    txt = persist_approved(
        _processed("hi", email_id="<m@h>"), "hi", out, "mbox:x"
    ).txt_path
    eml = next(out.glob("*.eml"))
    assert eml.stem == txt.stem


def test_start_imap_captures_account_routing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.cli import _cmd_start_imap

    email = _email(email_id="<imap@host>")
    monkeypatch.setattr("src.cli.load_imap_unread", lambda days: [email])
    monkeypatch.setenv("IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("IMAP_USER", "me@gmail.com")
    monkeypatch.setenv("IMAP_DRAFTS_FOLDER", "[Gmail]/Drafts")
    captured: dict[str, Any] = {}

    def _fake_run(
        emails: list[Email],
        sources: dict[str, str],
        args: argparse.Namespace,
        queue_dir: Path,
        imap_accounts: dict[str, ImapAccountRef] | None,
    ) -> int:
        captured.update(
            emails=emails,
            sources=sources,
            queue_dir=queue_dir,
            imap_accounts=imap_accounts,
        )
        return 0

    monkeypatch.setattr("src.cli._run_start_pipeline", _fake_run)
    rc = _cmd_start_imap(
        argparse.Namespace(queue_dir=str(tmp_path), days=14, limit=None)
    )
    assert rc == 0
    assert captured["sources"] == {email.id: "imap"}
    account = captured["imap_accounts"][email.id]
    assert account == ImapAccountRef(
        host="imap.gmail.com",
        user="me@gmail.com",
        drafts_folder="[Gmail]/Drafts",
    )


# ---------------------------------------------------------------------------
# Session records
# ---------------------------------------------------------------------------


def test_session_record_shape_is_json_serializable() -> None:
    p = _build_processed(
        _email(body="contact alice@example.com"),
        _result(),
        _decision(escalate=True),
        anonymizer=_FakeAnonymizer(),
        claude_client=_EchoClaude(),
        task="reply",
    )
    rec = session_record(
        p, action="approve", saved_path=Path("data/approved_drafts/x.txt")
    )
    # Round-trips through JSON (the log is JSONL).
    loaded = json.loads(json.dumps(rec))
    assert loaded["email_id"] == "<abc@host>"
    assert loaded["action"] == "approve"
    assert loaded["escalate"] is True
    assert loaded["provenance"] == "Claude"
    assert loaded["claude_used"] is True
    assert loaded["num_placeholders"] == 1
    assert loaded["approved_path"].endswith("x.txt")
    assert "timestamp" in loaded


def test_session_record_no_saved_path() -> None:
    rec = session_record(_processed("d"), action="reject", saved_path=None)
    assert rec["approved_path"] is None
    assert rec["action"] == "reject"


# ---------------------------------------------------------------------------
# processed_from_record  (review's rebuild of a stored queue record)
# ---------------------------------------------------------------------------


def test_processed_from_record_round_trip() -> None:
    from src.review_actions import processed_from_record
    from src.review_queue import QueueRecord

    p = _build_processed(
        _email(body="contact alice@example.com"),
        _result(),
        _decision(escalate=True),
        anonymizer=_FakeAnonymizer(),
        claude_client=_EchoClaude(),
        task="reply",
    )
    rec = QueueRecord(
        email=p.email,
        result=p.result,
        decision=p.decision,
        draft=p.draft,
        provenance=p.provenance,
        mapping=p.mapping,
        claude_used=p.claude_used,
        error=p.error,
        importance=7.0,
        importance_reason="urgent",
        ranked_by="Claude",
        source="mbox:x.mbox",
        processed_at="2026-06-09T10:00:00+00:00",
    )
    rebuilt = processed_from_record(rec)
    assert rebuilt == p


# ---------------------------------------------------------------------------
# reset  (clears the queue ledgers so `start` reprocesses everything)
# ---------------------------------------------------------------------------


def _queue_record(email_id: str = "<abc@host>") -> Any:
    from src.review_queue import QueueRecord

    return QueueRecord(
        email=_email(email_id=email_id),
        result=_result(),
        decision=_decision(escalate=False),
        draft="d",
        provenance="local",
        mapping={},
        claude_used=False,
        error=None,
        importance=5.0,
        importance_reason="",
        ranked_by="Claude",
        source="mbox:x.mbox",
        processed_at="2026-06-09T10:00:00+00:00",
    )


def test_reset_deletes_both_ledgers(tmp_path: Path) -> None:
    from src.cli import main
    from src.review_queue import (
        append_records,
        append_reviewed,
        processed_path,
        reviewed_path,
    )

    record = _queue_record()
    append_records(tmp_path, [record])
    append_reviewed(tmp_path, record.record_id, "<abc@host>", "approve", None)
    assert processed_path(tmp_path).exists() and reviewed_path(tmp_path).exists()

    rc = main(["reset", "--queue-dir", str(tmp_path), "--yes"])
    assert rc == 0
    assert not processed_path(tmp_path).exists()
    assert not reviewed_path(tmp_path).exists()


def test_reset_on_empty_queue_is_a_noop(tmp_path: Path) -> None:
    from src.cli import main

    rc = main(["reset", "--queue-dir", str(tmp_path), "--yes"])
    assert rc == 0


def test_reset_aborts_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.cli import main
    from src.review_queue import append_records, processed_path

    append_records(tmp_path, [_queue_record()])
    monkeypatch.setattr("src.cli.Prompt.ask", lambda *a, **k: "n")

    rc = main(["reset", "--queue-dir", str(tmp_path)])
    assert rc == 0
    assert processed_path(tmp_path).exists()  # nothing deleted


# ---------------------------------------------------------------------------
# _process_worker  (background half of the `process` command)
# ---------------------------------------------------------------------------
#
# The worker is run synchronously here — it's a plain function; the `process`
# command is what puts it on a thread. Order, the None sentinel, failure
# passthrough, and the stop event are what matter.


def _run_worker(
    emails: list[Email],
    *,
    stop: threading.Event | None = None,
    scorer: _FakeScorer | None = None,
) -> list[Any]:
    outcomes: queue.Queue = queue.Queue()
    _process_worker(
        emails,
        outcomes,
        stop or threading.Event(),
        ollama_client=None,  # triage is monkeypatched; the client is unused
        scorer=scorer or _FakeScorer(),
        anonymizer=_FakeAnonymizer(),
        task="reply",
    )
    items = []
    while not outcomes.empty():
        items.append(outcomes.get_nowait())
    return items


def test_worker_queues_outcomes_in_order_with_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.cli.triage", lambda email, client: _result())
    emails = [_email(email_id=f"<{n}@host>") for n in range(3)]
    items = _run_worker(emails)
    assert len(items) == 4
    assert [it.email.id for it in items[:3]] == [e.id for e in emails]
    assert all(it.processed is not None and it.error is None for it in items[:3])
    assert items[3] is None


def test_worker_passes_triage_failure_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(email: Email, client: Any) -> TriageResult:
        raise RuntimeError("ollama down")

    monkeypatch.setattr("src.cli.triage", _boom)
    items = _run_worker([_email()])
    assert items[0].processed is None
    assert isinstance(items[0].error, RuntimeError)
    assert items[1] is None


def test_worker_stop_event_skips_remaining_emails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.cli.triage", lambda email, client: _result())
    stop = threading.Event()
    stop.set()
    items = _run_worker([_email(), _email()], stop=stop)
    assert items == [None]  # sentinel only — nothing was processed


def test_worker_delegates_escalations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.cli.triage", lambda email, client: _result())
    monkeypatch.setattr("src.cli.ClaudeClient", _EchoClaude)
    email = _email(body="contact alice@example.com please", email_id="<1@h>")
    items = _run_worker([email], scorer=_FakeScorer(escalate_ids={"<1@h>"}))
    p = items[0].processed
    assert p.claude_used is True
    assert p.provenance == "Claude"
    assert "alice@example.com" in p.draft


def test_worker_routes_rehydrate_warning_into_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray placeholder in Claude's reply must not write to stderr from the
    worker thread (it would land inside the reviewer's prompt) — the warning
    travels through the queue as a note on the email that produced it."""

    class _StrayPlaceholderClaude:
        model = "fake-claude"

        def delegate(
            self, anonymized_email: str, anonymized_thread: Any, task: str
        ) -> str:
            return "Will do — looping in Ghost_P9."

    monkeypatch.setattr("src.cli.triage", lambda email, client: _result())
    monkeypatch.setattr("src.cli.ClaudeClient", _StrayPlaceholderClaude)
    emails = [_email(email_id="<1@h>"), _email(email_id="<2@h>")]
    items = _run_worker(emails, scorer=_FakeScorer(escalate_ids={"<2@h>"}))
    assert items[0].notes == []  # not escalated — no rehydration, no warning
    assert any("Ghost_P9" in n for n in items[1].notes)


def test_worker_restores_src_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.cli.triage", lambda email, client: _result())
    src_logger = logging.getLogger("src")
    handlers_before = list(src_logger.handlers)
    propagate_before = src_logger.propagate
    _run_worker([_email()])
    assert src_logger.handlers == handlers_before
    assert src_logger.propagate == propagate_before


def test_worker_notes_claude_init_failure_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_key() -> Any:
        raise RuntimeError("no api key")

    monkeypatch.setattr("src.cli.triage", lambda email, client: _result())
    monkeypatch.setattr("src.cli.ClaudeClient", _no_key)
    emails = [_email(email_id="<1@h>"), _email(email_id="<2@h>")]
    items = _run_worker(emails, scorer=_FakeScorer(escalate_ids={"<1@h>", "<2@h>"}))
    first, second = items[0], items[1]
    assert any("Claude client unavailable" in n for n in first.notes)
    assert second.notes == []  # warned once, not per email
    # Both fall back to the local draft.
    for it in (first, second):
        assert it.processed.provenance == "local"
        assert it.processed.error and "no Claude client" in it.processed.error
