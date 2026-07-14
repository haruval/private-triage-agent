"""Batch importance ranking for the review queue.

After `start` finishes a batch, one Claude request ranks every email 1–10 so
the summary table and the review order put urgent mail first. The privacy
contract is the same as delegation: the digest payload (subjects, summaries,
action items) is anonymized before it leaves the box, and Claude's per-email
reasons are rehydrated locally. One request for the whole batch — not one
per email — keeps the cost a rounding error.

If Claude is unavailable or returns something unparseable, the ranker falls
back to ordering by the router's escalation score. Review still works, just
with a cruder sort, and ``ranked_by`` says so.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from src.anonymize.rehydrate import rehydrate

logger = logging.getLogger(__name__)

MIN_IMPORTANCE = 1.0
MAX_IMPORTANCE = 10.0

_SUBJECT_CHARS = 100
_SUMMARY_CHARS = 240
_MAX_ACTION_ITEMS = 3
_ACTION_ITEM_CHARS = 90

RANKING_TASK = (
    "Below is a numbered list of triaged emails (category, summary, action "
    "items). Rate each email's importance to the recipient on a 1-10 scale: "
    "10 = urgent, must act today; 5 = should handle this week; 1 = ignorable. "
    "Respond with ONLY a JSON array, one object per email, like "
    '[{"id": 1, "importance": 7, "reason": "short why"}]. The "id" is the '
    "email's number in the list; include every email exactly once; keep each "
    "reason under 15 words."
)


@dataclass(frozen=True)
class EmailDigest:
    """The slice of one processed email that the ranking prompt needs."""

    email_id: str
    subject: str
    summary: str
    action_items: tuple[str, ...]
    category: str
    escalate: bool
    escalation_score: float


@dataclass(frozen=True)
class RankedEmail:
    importance: float  # clamped to [1, 10]
    reason: str


@dataclass
class ImportanceResult:
    scores: dict[str, RankedEmail]  # email_id -> ranking
    ranked_by: str                  # "Claude" or a fallback description


def rank_importance(
    digests: list[EmailDigest],
    *,
    claude_client: Any,
    anonymizer: Any,
) -> ImportanceResult:
    """Rank a batch of digests with one Claude call; degrade to a heuristic.

    ``claude_client`` may be None (no API key, offline) — every failure mode
    lands on the escalation-score fallback rather than raising, because a
    bad sort order should never block the pipeline.
    """
    if not digests:
        return ImportanceResult(scores={}, ranked_by="nothing to rank")
    if claude_client is None:
        return _fallback(digests, "escalation score (Claude unavailable)")

    try:
        anonymized, mapping = anonymizer.anonymize(_payload(digests))
        reply = claude_client.delegate(anonymized, None, RANKING_TASK)
        by_index = _parse_ranking(reply, len(digests))
    except Exception as exc:
        logger.warning("importance ranking failed (%s); using fallback", exc)
        return _fallback(digests, f"escalation score (ranking failed: {exc})")

    scores: dict[str, RankedEmail] = {}
    for i, digest in enumerate(digests, start=1):
        if i in by_index:
            importance, reason = by_index[i]
            scores[digest.email_id] = RankedEmail(
                importance=importance,
                reason=rehydrate(reason, mapping),
            )
        else:  # Claude skipped one — score it locally rather than dropping it
            scores[digest.email_id] = RankedEmail(
                importance=_heuristic_importance(digest),
                reason="not ranked by Claude; escalation-score fallback",
            )
    return ImportanceResult(scores=scores, ranked_by="Claude")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _clip(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _payload(digests: list[EmailDigest]) -> str:
    """Render the numbered digest list that gets anonymized and sent."""
    blocks: list[str] = []
    for i, d in enumerate(digests, start=1):
        lines = [
            f"{i}. Subject: {_clip(d.subject, _SUBJECT_CHARS) or '(no subject)'}",
            f"   Category: {d.category}"
            + ("  (flagged sensitive/escalated)" if d.escalate else ""),
            f"   Summary: {_clip(d.summary, _SUMMARY_CHARS)}",
        ]
        for item in d.action_items[:_MAX_ACTION_ITEMS]:
            lines.append(f"   Action item: {_clip(item, _ACTION_ITEM_CHARS)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _repair_truncated_array(text: str) -> str | None:
    """Close a cut-off JSON array at its last complete object.

    A max_tokens-truncated reply ends mid-entry; everything up to the last
    complete ``}`` is still valid, and a partial ranking beats the fallback.
    """
    end = text.rfind("}")
    if end == -1:
        return None
    return text[: end + 1] + "]"


def _parse_ranking(reply: str, count: int) -> dict[int, tuple[float, str]]:
    """Extract {index: (importance, reason)} from Claude's reply.

    Tolerates prose or code fences around the array, and a reply truncated
    mid-array; raises ValueError when nothing usable is found so the caller
    can fall back.
    """
    candidates: list[str] = []
    match = re.search(r"\[.*\]", reply, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    start = reply.find("[")
    if start != -1:
        repaired = _repair_truncated_array(reply[start:])
        if repaired:
            candidates.append(repaired)

    parsed: Any = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            break
        parsed = None
    if parsed is None:
        raise ValueError("no usable JSON array in ranking reply")

    by_index: dict[int, tuple[float, str]] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("id")
        importance = entry.get("importance")
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if not 1 <= idx <= count:
            continue
        if isinstance(importance, bool) or not isinstance(importance, (int, float)):
            continue
        clamped = min(MAX_IMPORTANCE, max(MIN_IMPORTANCE, float(importance)))
        by_index[idx] = (clamped, str(entry.get("reason", "")))
    if not by_index:
        raise ValueError("ranking reply had no valid entries")
    return by_index


def _heuristic_importance(digest: EmailDigest) -> float:
    score = min(1.0, max(0.0, digest.escalation_score))
    return round(MIN_IMPORTANCE + 9.0 * score, 1)


def _fallback(digests: list[EmailDigest], note: str) -> ImportanceResult:
    scores = {
        d.email_id: RankedEmail(
            importance=_heuristic_importance(d),
            reason=f"escalation score {d.escalation_score:.2f}",
        )
        for d in digests
    }
    return ImportanceResult(scores=scores, ranked_by=note)
