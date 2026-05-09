"""Tests for src/triage/classifier.py.

The Ollama client is mocked — these tests never touch a real model. They
cover both the validation logic in TriageResult.from_json_dict and the
prompt construction / wiring in the triage() function.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.ingestion.mbox_loader import Email
from src.triage.classifier import (
    SYSTEM_PROMPT,
    TriageResult,
    triage,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _valid_dict(**overrides) -> dict:
    base = {
        "category": "action_required",
        "confidence": 0.9,
        "summary": "Manager wants the deck reviewed by Friday.",
        "extracted_action_items": ["Review the deck", "Send comments by Friday"],
        "suggested_reply_draft": None,
        "reasoning": "Clear delegation with a deadline.",
    }
    base.update(overrides)
    return base


def _make_email(**overrides) -> Email:
    base = dict(
        id="<test1@example.com>",
        from_addr="alice@example.com",
        to_addrs=["bob@example.com"],
        subject="Quick question about the project",
        date=datetime(2024, 1, 12, 10, 30, tzinfo=timezone.utc),
        body_plain="Hi Bob, can you confirm the launch date?",
        thread_id=None,
        headers={},
    )
    base.update(overrides)
    return Email(**base)


# ---------------------------------------------------------------------------
# TriageResult.from_json_dict — happy paths
# ---------------------------------------------------------------------------


def test_from_json_dict_happy_path() -> None:
    result = TriageResult.from_json_dict(_valid_dict())
    assert isinstance(result, TriageResult)
    assert result.category == "action_required"
    assert result.confidence == 0.9
    assert result.extracted_action_items == [
        "Review the deck",
        "Send comments by Friday",
    ]
    assert result.suggested_reply_draft is None


def test_from_json_dict_keeps_draft_for_qualified_needs_reply() -> None:
    """needs_reply + confidence > 0.6 → draft is kept."""
    result = TriageResult.from_json_dict(
        _valid_dict(
            category="needs_reply",
            confidence=0.8,
            suggested_reply_draft="Wednesday works for me.",
        )
    )
    assert result.suggested_reply_draft == "Wednesday works for me."


def test_from_json_dict_defaults_action_items_when_missing() -> None:
    """The list field is allowed to be omitted; defaults to []."""
    d = _valid_dict()
    del d["extracted_action_items"]
    result = TriageResult.from_json_dict(d)
    assert result.extracted_action_items == []


# ---------------------------------------------------------------------------
# TriageResult.from_json_dict — coercion of orphan drafts
# ---------------------------------------------------------------------------


def test_orphan_draft_for_wrong_category_is_nulled_out() -> None:
    """A draft on a spam email is dropped, not raised — model is sometimes chatty."""
    result = TriageResult.from_json_dict(
        _valid_dict(
            category="spam",
            confidence=0.95,
            suggested_reply_draft="Thanks but no thanks.",
        )
    )
    assert result.suggested_reply_draft is None


def test_orphan_draft_for_low_confidence_is_nulled_out() -> None:
    """needs_reply but confidence at threshold → draft dropped."""
    result = TriageResult.from_json_dict(
        _valid_dict(
            category="needs_reply",
            confidence=0.6,  # not strictly > 0.6
            suggested_reply_draft="Sure, sounds good!",
        )
    )
    assert result.suggested_reply_draft is None


# ---------------------------------------------------------------------------
# TriageResult.from_json_dict — validation errors
# ---------------------------------------------------------------------------


def test_missing_category_raises_with_clear_message() -> None:
    d = _valid_dict()
    del d["category"]
    with pytest.raises(ValueError, match="category"):
        TriageResult.from_json_dict(d)


def test_invalid_category_value_raises() -> None:
    with pytest.raises(ValueError, match="Invalid category"):
        TriageResult.from_json_dict(_valid_dict(category="garbage"))


def test_confidence_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="confidence"):
        TriageResult.from_json_dict(_valid_dict(confidence=1.5))
    with pytest.raises(ValueError, match="confidence"):
        TriageResult.from_json_dict(_valid_dict(confidence=-0.1))


def test_confidence_wrong_type_raises() -> None:
    with pytest.raises(ValueError, match="confidence"):
        TriageResult.from_json_dict(_valid_dict(confidence="high"))


def test_empty_summary_raises() -> None:
    with pytest.raises(ValueError, match="summary"):
        TriageResult.from_json_dict(_valid_dict(summary="   "))


def test_action_items_with_non_string_raises() -> None:
    with pytest.raises(ValueError, match="extracted_action_items"):
        TriageResult.from_json_dict(
            _valid_dict(extracted_action_items=["ok", 42])
        )


def test_non_dict_input_raises() -> None:
    with pytest.raises(ValueError, match="dict"):
        TriageResult.from_json_dict("not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# triage() — wiring + prompt construction
# ---------------------------------------------------------------------------


def test_triage_calls_client_and_returns_parsed_result() -> None:
    client = MagicMock()
    client.generate_json.return_value = _valid_dict(
        category="needs_reply", confidence=0.85,
        suggested_reply_draft="Yes, the launch is March 14.",
    )
    email = _make_email()
    result = triage(email, client=client)

    client.generate_json.assert_called_once()
    assert isinstance(result, TriageResult)
    assert result.category == "needs_reply"
    assert result.suggested_reply_draft == "Yes, the launch is March 14."


def test_triage_user_prompt_contains_email_fields() -> None:
    client = MagicMock()
    client.generate_json.return_value = _valid_dict()
    email = _make_email(
        from_addr="distinct.sender@example.com",
        subject="Distinctive Subject Line 12345",
        body_plain="Body content with a unique string XQZ-7.",
    )
    triage(email, client=client)

    kwargs = client.generate_json.call_args.kwargs
    prompt = kwargs["prompt"]
    assert "distinct.sender@example.com" in prompt
    assert "Distinctive Subject Line 12345" in prompt
    assert "XQZ-7" in prompt


def test_triage_system_prompt_has_few_shot_examples() -> None:
    """System prompt should embed multiple JSON example outputs."""
    client = MagicMock()
    client.generate_json.return_value = _valid_dict(category="spam", confidence=0.97)
    triage(_make_email(), client=client)

    kwargs = client.generate_json.call_args.kwargs
    system = kwargs["system"]

    # Mentions every category at least once
    for cat in ("action_required", "needs_reply", "fyi", "spam", "unclear"):
        assert cat in system, f"category {cat!r} missing from system prompt"

    # Has at least 2 example JSON outputs (each starts with the category key)
    assert system.count('"category"') >= 3, "should have 3 example outputs"
    # Sanity check it matches the constant
    assert system == SYSTEM_PROMPT


def test_triage_truncates_very_long_bodies() -> None:
    """A body > MAX_BODY_CHARS should be truncated in the prompt."""
    client = MagicMock()
    client.generate_json.return_value = _valid_dict()
    huge_body = "x" * 5000 + "TAIL_MARKER"
    triage(_make_email(body_plain=huge_body), client=client)

    prompt = client.generate_json.call_args.kwargs["prompt"]
    assert "TAIL_MARKER" not in prompt
    assert "[...truncated...]" in prompt
