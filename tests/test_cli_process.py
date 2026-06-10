"""Tests for the `process` command machinery in src/cli.py.

The pipeline glue is exercised with injected fakes — no Ollama, no spaCy, no
Claude network call. (Project policy is to use the real Claude API for the
delegate/eval tests; here we're testing the CLI's orchestration, fallback, and
file-writing logic, which is pure local code.) The escalation path is verified
end-to-end with a fake anonymizer + fake Claude so the anonymize → delegate →
rehydrate round-trip is checked without a model.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.cli import (
    ProcessedEmail,
    _build_processed,
    _email_payload,
    _process_worker,
    _safe_stem,
    _save_approved_draft,
    _session_record,
)
from src.ingestion.mbox_loader import Email
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
        assert "Email_E1" in anonymized_email  # the PII was anonymized before delegation
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


def _email(body: str = "ping", subject: str = "Hi", email_id: str = "<abc@host>") -> Email:
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


def _result(draft: str | None = "local draft", category: str = "needs_reply") -> TriageResult:
    return TriageResult(
        category=category,
        confidence=0.9,
        summary="s",
        extracted_action_items=["do x"],
        suggested_reply_draft=draft,
        reasoning="r",
    )


def _decision(escalate: bool) -> EscalationDecision:
    return EscalationDecision(escalate=escalate, reason="because", score=0.6 if escalate else 0.1)


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
    stem = _safe_stem(_email(email_id="<a/b c@host>"))
    assert "/" not in stem and " " not in stem and "<" not in stem
    assert stem  # non-empty


def test_save_approved_draft_writes_header_and_body(tmp_path: Path) -> None:
    out = tmp_path / "approved"
    path = _save_approved_draft(_processed("Hello there."), "Hello there.", out)
    assert path.exists()
    assert path.parent == out
    content = path.read_text()
    assert "Subject: Re: Hi" in content
    assert "X-Draft-Provenance: local" in content
    assert content.rstrip().endswith("Hello there.")


def test_save_approved_draft_does_not_clobber(tmp_path: Path) -> None:
    out = tmp_path / "approved"
    p1 = _save_approved_draft(_processed("one"), "one", out)
    p2 = _save_approved_draft(_processed("two"), "two", out)
    assert p1 != p2
    assert p1.exists() and p2.exists()
    assert "one" in p1.read_text()
    assert "two" in p2.read_text()


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
    rec = _session_record(p, action="approve", saved_path=Path("data/approved_drafts/x.txt"))
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
    rec = _session_record(_processed("d"), action="reject", saved_path=None)
    assert rec["approved_path"] is None
    assert rec["action"] == "reject"


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

        def delegate(self, anonymized_email: str, anonymized_thread: Any, task: str) -> str:
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
