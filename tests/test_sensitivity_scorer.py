"""Tests for src/router/sensitivity_scorer.py.

Pure logic — no network. Config validation, the individual escalation signals,
their combination, and the sender override. One test loads the real
configs/router.yaml to make sure the shipped config stays valid and that the
README's legal+money example actually escalates.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.ingestion.mbox_loader import Email
from src.router.sensitivity_scorer import (
    DEFAULT_CONFIG_PATH,
    EscalationDecision,
    RouterConfig,
    SensitivityScorer,
    _thread_length,
)
from src.triage.classifier import TriageResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _email(
    *,
    body: str = "",
    subject: str = "hi",
    from_addr: str = "sender@example.com",
    headers: dict[str, str] | None = None,
) -> Email:
    return Email(
        id="e1",
        from_addr=from_addr,
        to_addrs=["me@example.com"],
        subject=subject,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        body_plain=body,
        thread_id=None,
        headers=headers or {},
    )


def _result(
    *,
    confidence: float = 0.95,
    category: str = "needs_reply",
    summary: str = "a summary",
    items: list[str] | None = None,
) -> TriageResult:
    return TriageResult(
        category=category,
        confidence=confidence,
        summary=summary,
        extracted_action_items=items or [],
        suggested_reply_draft=None,
        reasoning="r",
    )


def _config(**overrides) -> RouterConfig:
    """A small, predictable config for logic tests."""
    base = {
        "threshold": 0.5,
        "confidence_floor": 0.8,
        "thread_length_threshold": 4,
        "weights": {
            "low_confidence": 0.5,
            "unclear_category": 0.5,
            "long_thread": 0.2,
            "keyword_legal": 0.5,
            "keyword_negotiation": 0.4,
            "keyword_technical": 0.25,
        },
        "keywords": {
            "legal": ["contract", "liability"],
            "negotiation": ["counteroffer", "renewal"],
            "technical": ["database", "deployment"],
        },
        "always_escalate_senders": [],
    }
    base.update(overrides)
    return RouterConfig.from_dict(base)


# ---------------------------------------------------------------------------
# RouterConfig validation
# ---------------------------------------------------------------------------


def test_config_from_dict_valid() -> None:
    cfg = _config()
    assert cfg.threshold == 0.5
    assert cfg.weight("keyword_legal") == 0.5
    assert cfg.weight("nonexistent") == 0.0  # absent → 0
    assert cfg.keywords["legal"] == ["contract", "liability"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"threshold": 1.5},                       # out of [0, 1]
        {"confidence_floor": -0.1},               # out of [0, 1]
        {"thread_length_threshold": 0},           # must be >= 1
        {"thread_length_threshold": True},        # bool rejected
        {"weights": {"low_confidence": -1}},      # negative weight
        {"weights": {"low_confidence": "high"}},  # non-numeric weight
        {"keywords": {"legal": "contract"}},      # not a list
        {"keywords": {"legal": [1, 2]}},          # not strings
        {"always_escalate_senders": "boss@x.com"},  # not a list
    ],
)
def test_config_from_dict_invalid(overrides: dict) -> None:
    base = {
        "threshold": 0.5,
        "confidence_floor": 0.8,
        "thread_length_threshold": 4,
        "weights": {"low_confidence": 0.5},
        "keywords": {},
        "always_escalate_senders": [],
    }
    base.update(overrides)
    with pytest.raises(ValueError):
        RouterConfig.from_dict(base)


def test_config_missing_required_field() -> None:
    with pytest.raises(ValueError, match="threshold"):
        RouterConfig.from_dict({"confidence_floor": 0.8})


def test_shipped_config_loads_and_is_valid() -> None:
    cfg = RouterConfig.load(DEFAULT_CONFIG_PATH)
    assert 0.0 <= cfg.threshold <= 1.0
    assert "legal" in cfg.keywords
    assert cfg.weight("keyword_legal") > 0


# ---------------------------------------------------------------------------
# Individual signals
# ---------------------------------------------------------------------------


def test_confident_benign_email_does_not_escalate() -> None:
    scorer = SensitivityScorer(config=_config())
    d = scorer.score(_email(body="lunch tuesday?"), _result())
    assert isinstance(d, EscalationDecision)
    assert d.escalate is False
    assert d.score == 0.0
    assert d.reason == "no escalation signals"


def test_low_confidence_escalates() -> None:
    scorer = SensitivityScorer(config=_config())
    d = scorer.score(_email(body="hm"), _result(confidence=0.55))
    assert d.escalate is True
    assert d.score == pytest.approx(0.5)
    assert "low local confidence" in d.reason


def test_unclear_category_escalates() -> None:
    scorer = SensitivityScorer(config=_config())
    d = scorer.score(_email(body="???"), _result(category="unclear", confidence=0.95))
    assert d.escalate is True
    assert "unclear" in d.reason


def test_legal_keyword_alone_escalates() -> None:
    scorer = SensitivityScorer(config=_config())
    d = scorer.score(
        _email(body="Please review the contract and the liability cap."), _result()
    )
    assert d.escalate is True
    assert d.score == pytest.approx(0.5)
    assert "legal keywords" in d.reason
    assert "contract" in d.reason


def test_technical_keyword_alone_does_not_escalate() -> None:
    # technical weight (0.25) is below threshold (0.5) on its own.
    scorer = SensitivityScorer(config=_config())
    d = scorer.score(_email(body="the database migration is done"), _result())
    assert d.escalate is False
    assert d.score == pytest.approx(0.25)
    assert "technical keywords" in d.reason


def test_keyword_matches_at_word_boundary_only() -> None:
    scorer = SensitivityScorer(config=_config())
    # "contractor" matches the "contract" prefix at a word boundary → legal fires.
    d = scorer.score(_email(body="the contractor arrives monday"), _result())
    assert "legal keywords" in d.reason
    # "noncontractual" has "contract" mid-word (no left boundary) → must NOT fire.
    d2 = scorer.score(_email(body="just a noncontractual note", subject="x"), _result())
    assert "legal keywords" not in d2.reason


def test_combined_signals_sum_and_cap() -> None:
    scorer = SensitivityScorer(config=_config())
    # negotiation (0.4) alone < threshold...
    d = scorer.score(_email(body="counteroffer on the renewal"), _result())
    assert d.escalate is False
    assert d.score == pytest.approx(0.4)
    # ...negotiation (0.4) + long thread (0.2) = 0.6 >= 0.5 → escalate.
    headers = {"References": "<a@x> <b@x> <c@x> <d@x>"}
    d2 = scorer.score(
        _email(body="counteroffer on the renewal", headers=headers), _result()
    )
    assert d2.escalate is True
    assert d2.score == pytest.approx(0.6)
    assert "long thread" in d2.reason


def test_score_is_capped_at_one() -> None:
    scorer = SensitivityScorer(config=_config())
    headers = {"References": "<a@x> <b@x> <c@x> <d@x>"}
    d = scorer.score(
        _email(body="contract liability counteroffer renewal database", headers=headers),
        _result(confidence=0.1, category="unclear"),
    )
    assert d.score == 1.0
    assert d.escalate is True


# ---------------------------------------------------------------------------
# Sender override
# ---------------------------------------------------------------------------


def test_sender_override_exact_match() -> None:
    scorer = SensitivityScorer(config=_config(always_escalate_senders=["boss@corp.com"]))
    d = scorer.score(_email(from_addr="Boss@Corp.com", body="fyi"), _result())
    assert d.escalate is True
    assert d.score == 1.0
    assert "override" in d.reason


def test_sender_override_domain_match() -> None:
    scorer = SensitivityScorer(config=_config(always_escalate_senders=["@legal.corp.com"]))
    d = scorer.score(_email(from_addr="paralegal@legal.corp.com", body="fyi"), _result())
    assert d.escalate is True
    assert d.score == 1.0


def test_sender_override_no_false_match() -> None:
    scorer = SensitivityScorer(config=_config(always_escalate_senders=["boss@corp.com"]))
    d = scorer.score(_email(from_addr="someone@corp.com", body="lunch?"), _result())
    assert d.escalate is False


# ---------------------------------------------------------------------------
# Thread length estimation
# ---------------------------------------------------------------------------


def test_thread_length_from_references() -> None:
    e = _email(headers={"References": "<a@x> <b@x> <c@x>"})
    assert _thread_length(e) == 4  # 3 ancestors + this message


def test_thread_length_from_in_reply_to() -> None:
    e = _email(headers={"In-Reply-To": "<a@x>"})
    assert _thread_length(e) == 2


def test_thread_length_default_is_one() -> None:
    assert _thread_length(_email(headers={})) == 1


def test_thread_length_header_lookup_is_case_insensitive() -> None:
    e = _email(headers={"references": "<a@x> <b@x>"})
    assert _thread_length(e) == 3


# ---------------------------------------------------------------------------
# End-to-end against the shipped config (offline)
# ---------------------------------------------------------------------------


def test_readme_example_escalates_with_shipped_config() -> None:
    """The README's contract-renewal email should escalate on real config."""
    scorer = SensitivityScorer()  # loads configs/router.yaml
    email = _email(
        subject="Contract renewal - need your sign-off by Friday",
        body=(
            "Legal flagged two changes to the liability cap and we should push "
            "back before signing. Can you review the redlines and confirm the "
            "$250,000 figure before Friday?"
        ),
    )
    d = scorer.score(email, _result(confidence=0.85, category="action_required"))
    assert d.escalate is True
    assert "legal keywords" in d.reason
