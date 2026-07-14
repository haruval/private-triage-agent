"""Tests for the batch importance ranker (src/router/importance.py).

Fakes only — the ranker's contract is that it never raises: every failure
mode (no client, API error, junk JSON, missing entries) lands on the
escalation-score fallback. The privacy edge is checked too: the payload that
reaches the fake Claude is the anonymized one, and reasons are rehydrated.
"""

from __future__ import annotations

import json
from typing import Any

from src.router.importance import EmailDigest, rank_importance


def _digest(
    email_id: str = "<a@h>",
    escalate: bool = False,
    score: float = 0.2,
    summary: str = "Carol asked for the contract.",
) -> EmailDigest:
    return EmailDigest(
        email_id=email_id,
        subject="Contract question",
        summary=summary,
        action_items=("send the contract",),
        category="needs_reply",
        escalate=escalate,
        escalation_score=score,
    )


class _PassthroughAnonymizer:
    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        return text, {}


class _NameAnonymizer:
    """Replaces 'Carol' like the real anonymizers would."""

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        return text.replace("Carol", "Alex_P1"), {"Alex_P1": "Carol"}


class _FakeClaude:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_payload: str | None = None

    def delegate(self, anonymized_email: str, anonymized_thread: Any, task: str) -> str:
        self.last_payload = anonymized_email
        return self.reply


class _BoomClaude:
    def delegate(self, *a: Any, **k: Any) -> str:
        raise RuntimeError("api exploded")


def test_parses_ranking_with_prose_and_fences() -> None:
    reply = (
        "Here is the ranking you asked for:\n```json\n"
        + json.dumps(
            [
                {"id": 1, "importance": 9, "reason": "urgent"},
                {"id": 2, "importance": 3, "reason": "informational"},
            ]
        )
        + "\n```\nLet me know if you need more."
    )
    digests = [_digest("<a@h>"), _digest("<b@h>")]
    result = rank_importance(
        digests,
        claude_client=_FakeClaude(reply),
        anonymizer=_PassthroughAnonymizer(),
    )
    assert result.ranked_by == "Claude"
    assert result.scores["<a@h>"].importance == 9.0
    assert result.scores["<b@h>"].importance == 3.0
    assert result.scores["<a@h>"].reason == "urgent"


def test_importance_is_clamped_to_1_10() -> None:
    reply = json.dumps(
        [
            {"id": 1, "importance": 15, "reason": "over"},
            {"id": 2, "importance": -2, "reason": "under"},
        ]
    )
    digests = [_digest("<a@h>"), _digest("<b@h>")]
    result = rank_importance(
        digests,
        claude_client=_FakeClaude(reply),
        anonymizer=_PassthroughAnonymizer(),
    )
    assert result.scores["<a@h>"].importance == 10.0
    assert result.scores["<b@h>"].importance == 1.0


def test_email_skipped_by_claude_gets_heuristic_score() -> None:
    reply = json.dumps([{"id": 1, "importance": 8, "reason": "ranked"}])
    digests = [_digest("<a@h>"), _digest("<b@h>", escalate=True, score=1.0)]
    result = rank_importance(
        digests,
        claude_client=_FakeClaude(reply),
        anonymizer=_PassthroughAnonymizer(),
    )
    assert result.ranked_by == "Claude"
    assert result.scores["<a@h>"].importance == 8.0
    assert result.scores["<b@h>"].importance == 10.0  # 1 + 9 * score
    assert "fallback" in result.scores["<b@h>"].reason


def test_no_client_falls_back_to_escalation_score() -> None:
    digests = [_digest("<a@h>", score=0.5)]
    result = rank_importance(
        digests, claude_client=None, anonymizer=_PassthroughAnonymizer()
    )
    assert "unavailable" in result.ranked_by
    assert result.scores["<a@h>"].importance == 5.5  # 1 + 9 * 0.5


def test_api_error_falls_back() -> None:
    digests = [_digest("<a@h>", score=1.0)]
    result = rank_importance(
        digests, claude_client=_BoomClaude(), anonymizer=_PassthroughAnonymizer()
    )
    assert "ranking failed" in result.ranked_by
    assert result.scores["<a@h>"].importance == 10.0


def test_truncated_reply_is_salvaged() -> None:
    """A max_tokens-truncated array (no closing ]) keeps the complete entries."""
    reply = (
        '[{"id": 1, "importance": 9, "reason": "urgent"},'
        ' {"id": 2, "importance": 4, "reason": "routine"},'
        ' {"id": 3, "importance"'  # cut off mid-entry
    )
    digests = [
        _digest("<a@h>"),
        _digest("<b@h>"),
        _digest("<c@h>", escalate=True, score=1.0),
    ]
    result = rank_importance(
        digests,
        claude_client=_FakeClaude(reply),
        anonymizer=_PassthroughAnonymizer(),
    )
    assert result.ranked_by == "Claude"
    assert result.scores["<a@h>"].importance == 9.0
    assert result.scores["<b@h>"].importance == 4.0
    # The entry that was cut off gets the heuristic score instead.
    assert result.scores["<c@h>"].importance == 10.0
    assert "fallback" in result.scores["<c@h>"].reason


def test_junk_reply_falls_back() -> None:
    digests = [_digest("<a@h>")]
    result = rank_importance(
        digests,
        claude_client=_FakeClaude("I cannot rank these emails."),
        anonymizer=_PassthroughAnonymizer(),
    )
    assert "ranking failed" in result.ranked_by


def test_payload_is_anonymized_and_reasons_are_rehydrated() -> None:
    reply = json.dumps([{"id": 1, "importance": 7, "reason": "Alex_P1 is waiting"}])
    claude = _FakeClaude(reply)
    digests = [_digest("<a@h>", summary="Carol asked for the contract.")]
    result = rank_importance(
        digests, claude_client=claude, anonymizer=_NameAnonymizer()
    )
    # What left the box had the placeholder, not the name…
    assert "Carol" not in claude.last_payload
    assert "Alex_P1" in claude.last_payload
    # …and the reason shown to the user has the name back.
    assert result.scores["<a@h>"].reason == "Carol is waiting"


def test_empty_batch() -> None:
    result = rank_importance(
        [], claude_client=None, anonymizer=_PassthroughAnonymizer()
    )
    assert result.scores == {}
