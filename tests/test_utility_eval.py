"""Tests for src/eval/utility_eval.py.

The Claude calls are REAL (key from .env via conftest); the integration test
self-skips without a key. The local judge (gemma3:27b) is stubbed with a
deterministic scorer so the suite stays fast and the assertions stay stable —
the request was to stop faking *Claude*, not to spin up a 27B judge on every
test run. The full real-judge path is exercised when the eval is actually run.

Pure-logic tests (rubric validation, table building) and error-path tests
(fault-injected failures) need no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.anonymize.regex_anonymizer import RegexAnonymizer
from src.eval.utility_eval import (
    PIPELINES,
    RubricScores,
    _email_text,
    build_summary_table,
    run_utility_eval,
)
from src.ingestion.mbox_loader import Email

from tests.conftest import requires_anthropic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _email(email_id: str = "e1", body: str = "Please confirm by 2024-12-01 and reply to alice@example.com.") -> Email:
    return Email(
        id=email_id,
        from_addr="john@example.com",
        to_addrs=["me@example.com"],
        subject="confirm?",
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        body_plain=body,
        thread_id=None,
        headers={},
    )


class _StubJudge:
    """Deterministic local-judge stand-in (Claude is the one that must be real)."""

    model = "gemma3:27b-stub"

    def __init__(self, scores: dict[str, Any] | None = None) -> None:
        self._scores = scores or {
            "relevance": 4,
            "specificity": 3,
            "actionability": 5,
            "naturalness": 4,
            "justification": "stub",
        }

    def generate_json(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        return dict(self._scores)


class _StubFullAnon:
    """Stand-in for the regex+NER+coref anonymizer, so tests don't load spaCy
    or fastcoref. Maps a couple of tokens to placeholders."""

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        out = text.replace("alice@example.com", "Email_E1").replace("2024-12-01", "Date_D1")
        return out, {"Email_E1": "alice@example.com", "Date_D1": "2024-12-01"}


# ---------------------------------------------------------------------------
# RubricScores validation (pure)
# ---------------------------------------------------------------------------


def test_rubric_scores_mean() -> None:
    assert RubricScores(4, 3, 5, 4).mean == pytest.approx(4.0)


def test_rubric_from_json_dict_valid() -> None:
    s = RubricScores.from_json_dict(
        {"relevance": 5, "specificity": 4, "actionability": 3, "naturalness": 2, "justification": "ok"}
    )
    assert (s.relevance, s.specificity, s.actionability, s.naturalness) == (5.0, 4.0, 3.0, 2.0)
    assert s.justification == "ok"


@pytest.mark.parametrize(
    "bad",
    [
        {"specificity": 4, "actionability": 3, "naturalness": 2},  # missing relevance
        {"relevance": 6, "specificity": 4, "actionability": 3, "naturalness": 2},  # > 5
        {"relevance": 0, "specificity": 4, "actionability": 3, "naturalness": 2},  # < 1
        {"relevance": "high", "specificity": 4, "actionability": 3, "naturalness": 2},  # wrong type
        {"relevance": True, "specificity": 4, "actionability": 3, "naturalness": 2},  # bool rejected
    ],
)
def test_rubric_from_json_dict_invalid(bad: dict) -> None:
    with pytest.raises(ValueError):
        RubricScores.from_json_dict(bad)


# ---------------------------------------------------------------------------
# Live end-to-end driver (real Claude, stub judge)
# ---------------------------------------------------------------------------


@requires_anthropic
@pytest.mark.integration
def test_run_utility_eval_real_claude(tmp_path: Path) -> None:
    from src.delegate.claude_client import ClaudeClient

    email = _email()
    claude = ClaudeClient(log_path=tmp_path / "claude.jsonl", max_tokens=256)
    log = tmp_path / "util.jsonl"

    results, out_path = run_utility_eval(
        [email],
        claude=claude,
        judge=_StubJudge(),
        regex_anon=RegexAnonymizer(),
        full_anon=_StubFullAnon(),
        log_path=log,
    )

    assert out_path == log
    assert len(results) == 1
    r = results[0]
    assert [pr.pipeline for pr in r.results] == list(PIPELINES)

    by_pipeline = {pr.pipeline: pr for pr in r.results}

    # Every pipeline got a non-empty draft from the real API and a judge score.
    for pr in r.results:
        assert pr.draft and pr.draft.strip(), f"empty draft for {pr.pipeline}"
        assert pr.scores is not None, f"no score for {pr.pipeline}"
        assert pr.error is None

    # raw sends no placeholders.
    assert by_pipeline["raw"].num_placeholders == 0

    # regex placeholder count matches what the anonymizer actually produced,
    # and the rehydration invariant holds: no known placeholder survives.
    _, regex_map = RegexAnonymizer().anonymize(_email_text(email))
    assert by_pipeline["regex"].num_placeholders == len(regex_map)
    for placeholder in regex_map:
        assert placeholder not in by_pipeline["regex"].draft

    # full pipeline (stubbed anonymizer) maps two tokens; both rehydrated away.
    assert by_pipeline["full"].num_placeholders == 2
    assert "Email_E1" not in by_pipeline["full"].draft
    assert "Date_D1" not in by_pipeline["full"].draft

    # JSONL: one record per pipeline.
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 3
    rec = json.loads(lines[0])
    assert rec["email_id"] == "e1"
    assert rec["claude_model"] == claude.model
    assert rec["judge_model"] == "gemma3:27b-stub"


# ---------------------------------------------------------------------------
# Error paths — fault injection (no network)
# ---------------------------------------------------------------------------


def test_judge_failure_is_captured_not_raised(tmp_path: Path) -> None:
    class _BadJudge:
        model = "judge"

        def generate_json(self, prompt: str, system: str | None = None) -> dict:
            return {"relevance": 99}  # invalid → from_json_dict raises

    class _OkClaude:
        model = "claude"

        def delegate(self, *a: Any, **k: Any) -> str:
            return "a draft"

    results, _ = run_utility_eval(
        [_email()],
        claude=_OkClaude(),
        judge=_BadJudge(),
        regex_anon=RegexAnonymizer(),
        full_anon=_StubFullAnon(),
        log_path=tmp_path / "util.jsonl",
    )
    prs = results[0].results
    assert all(pr.scores is None for pr in prs)
    assert all(pr.error and pr.error.startswith("judge:") for pr in prs)
    assert all(pr.draft is not None for pr in prs)  # drafts still produced


def test_delegate_failure_is_captured_not_raised(tmp_path: Path) -> None:
    class _BadClaude:
        model = "claude"

        def delegate(self, *a: Any, **k: Any) -> str:
            raise RuntimeError("api exploded")

    results, _ = run_utility_eval(
        [_email()],
        claude=_BadClaude(),
        judge=_StubJudge(),
        regex_anon=RegexAnonymizer(),
        full_anon=_StubFullAnon(),
        log_path=tmp_path / "util.jsonl",
    )
    prs = results[0].results
    assert all(pr.draft is None for pr in prs)
    assert all(pr.error and pr.error.startswith("delegate:") for pr in prs)
    assert all(pr.scores is None for pr in prs)


# ---------------------------------------------------------------------------
# Summary table (pure)
# ---------------------------------------------------------------------------


def test_build_summary_table_has_a_row_per_pipeline() -> None:
    from src.eval.utility_eval import EmailEvalResult, PipelineResult

    def mk(r: float, s: float, a: float, n: float) -> RubricScores:
        return RubricScores(r, s, a, n, "x")

    results = [
        EmailEvalResult("e1", "s", [
            PipelineResult("raw", "d", 0, mk(5, 5, 5, 5)),
            PipelineResult("regex", "d", 2, mk(4, 4, 4, 4)),
            PipelineResult("full", None, 3, None, error="judge: bad"),
        ]),
    ]
    table = build_summary_table(results)
    assert table.row_count == len(PIPELINES)
    assert len(table.columns) == 4 + 4  # pipeline + 4 axes + mean + n + errors
