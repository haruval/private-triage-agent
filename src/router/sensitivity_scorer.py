"""Sensitivity scoring + escalation decision.

The local model triages every email; this module decides whether a given
email is worth a second, stronger opinion from Claude (after anonymization).
It combines a handful of cheap signals into one score and compares it to a
threshold:

- low local confidence (the model wasn't sure)
- an ``unclear`` category (the model couldn't classify it)
- keyword hits in configurable categories (legal / financial / negotiation /
  sensitive / technical)
- a long thread (lots of back-and-forth tends to carry nuance)
- an explicit always-escalate sender override (hard escalate, score 1.0)

Every weight, threshold, and keyword list lives in ``configs/router.yaml`` —
this file holds no magic numbers. The score is a plain sum of the weights of
the signals that fired, capped at 1.0; ``escalate`` is ``score >= threshold``
(or a sender override). The deliberately simple, explainable math beats a
fancier model here: the ``reason`` string has to be readable by a human
reviewing the decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.ingestion.mbox_loader import Email
from src.triage.classifier import TriageResult

DEFAULT_CONFIG_PATH = Path("configs/router.yaml")


# ---------------------------------------------------------------------------
# Decision + config dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationDecision:
    """Whether to send an email to Claude, with a human-readable rationale."""

    escalate: bool
    reason: str
    score: float


@dataclass
class RouterConfig:
    """Validated view of ``configs/router.yaml``."""

    threshold: float
    confidence_floor: float
    thread_length_threshold: int
    weights: dict[str, float]
    keywords: dict[str, list[str]]
    always_escalate_senders: list[str]

    def weight(self, name: str) -> float:
        """Weight for a signal name; 0.0 if the config doesn't mention it."""
        return float(self.weights.get(name, 0.0))

    # --- validation / loading --------------------------------------------

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RouterConfig":
        if not isinstance(d, dict):
            raise ValueError(f"router config must be a mapping, got {type(d).__name__}")

        threshold = _require_unit_float(d, "threshold")
        confidence_floor = _require_unit_float(d, "confidence_floor")

        tlt = d.get("thread_length_threshold", 4)
        if isinstance(tlt, bool) or not isinstance(tlt, int) or tlt < 1:
            raise ValueError(
                f"'thread_length_threshold' must be an int >= 1, got {tlt!r}"
            )

        weights_raw = d.get("weights", {})
        if not isinstance(weights_raw, dict):
            raise ValueError("'weights' must be a mapping of signal -> number")
        weights: dict[str, float] = {}
        for k, v in weights_raw.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"weight {k!r} must be a number >= 0, got {v!r}")
            weights[str(k)] = float(v)

        keywords_raw = d.get("keywords", {})
        if not isinstance(keywords_raw, dict):
            raise ValueError("'keywords' must be a mapping of category -> list[str]")
        keywords: dict[str, list[str]] = {}
        for cat, words in keywords_raw.items():
            if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
                raise ValueError(f"keywords[{cat!r}] must be a list of strings")
            keywords[str(cat)] = list(words)

        senders_raw = d.get("always_escalate_senders", [])
        if not isinstance(senders_raw, list) or not all(
            isinstance(s, str) for s in senders_raw
        ):
            raise ValueError("'always_escalate_senders' must be a list of strings")

        return cls(
            threshold=threshold,
            confidence_floor=confidence_floor,
            thread_length_threshold=tlt,
            weights=weights,
            keywords=keywords,
            always_escalate_senders=list(senders_raw),
        )

    @classmethod
    def load(cls, path: Path | str | None = None) -> "RouterConfig":
        p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        if not p.exists():
            raise FileNotFoundError(
                f"router config not found: {p}. Expected a YAML file like "
                f"configs/router.yaml (see the repo for the default)."
            )
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)


def _require_unit_float(d: dict[str, Any], key: str) -> float:
    if key not in d:
        raise ValueError(f"missing required field: {key!r}")
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{key!r} must be a number, got {type(v).__name__}")
    f = float(v)
    if not 0.0 <= f <= 1.0:
        raise ValueError(f"{key!r} must be in [0, 1], got {f}")
    return f


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


class SensitivityScorer:
    """Score an ``Email`` + ``TriageResult`` into an :class:`EscalationDecision`.

    Pass an explicit :class:`RouterConfig` (handy for tests), or let the scorer
    load ``configs/router.yaml`` (override the path with ``config_path``).
    """

    def __init__(
        self,
        config: RouterConfig | None = None,
        *,
        config_path: Path | str | None = None,
    ) -> None:
        self.config = config if config is not None else RouterConfig.load(config_path)

    def score(self, email: Email, result: TriageResult) -> EscalationDecision:
        cfg = self.config

        # Sender override is a hard escalate — short-circuit before scoring.
        override = _matched_sender(email.from_addr, cfg.always_escalate_senders)
        if override is not None:
            return EscalationDecision(
                escalate=True,
                reason=f"always-escalate sender override ({override})",
                score=1.0,
            )

        score = 0.0
        reasons: list[str] = []

        def fire(weight: float, fragment: str) -> None:
            nonlocal score
            if weight > 0:
                score += weight
                reasons.append(fragment)

        if result.confidence < cfg.confidence_floor:
            fire(
                cfg.weight("low_confidence"),
                f"low local confidence ({result.confidence:.2f} < "
                f"{cfg.confidence_floor:.2f})",
            )

        if result.category == "unclear":
            fire(cfg.weight("unclear_category"), "local model returned 'unclear'")

        haystack = "\n".join(
            [
                email.subject or "",
                email.body_plain or "",
                result.summary or "",
                *result.extracted_action_items,
            ]
        ).lower()
        for category, words in cfg.keywords.items():
            matched = _matched_keywords(haystack, words)
            if matched:
                shown = ", ".join(matched[:4])
                fire(cfg.weight(f"keyword_{category}"), f"{category} keywords ({shown})")

        thread_len = _thread_length(email)
        if thread_len >= cfg.thread_length_threshold:
            fire(cfg.weight("long_thread"), f"long thread (~{thread_len} messages)")

        score = min(1.0, score)
        escalate = score >= cfg.threshold
        reason = "; ".join(reasons) if reasons else "no escalation signals"
        return EscalationDecision(escalate=escalate, reason=reason, score=round(score, 3))


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------


def _matched_keywords(haystack: str, words: list[str]) -> list[str]:
    """Return the config keywords that appear in ``haystack``.

    Matching is at a left word boundary, so "contract" also catches
    "contracts"/"contractual" but not a mid-word coincidence. ``haystack`` is
    expected to already be lowercased.
    """
    out: list[str] = []
    for kw in words:
        term = kw.lower().strip()
        if not term:
            continue
        if re.search(r"\b" + re.escape(term), haystack):
            out.append(kw)
    return out


def _matched_sender(from_addr: str | None, entries: list[str]) -> str | None:
    """Return the matching override entry, or None.

    An entry beginning with "@" matches any address at that domain; otherwise
    the match is exact. Case-insensitive.
    """
    sender = (from_addr or "").strip().lower()
    if not sender:
        return None
    for entry in entries:
        e = entry.strip().lower()
        if not e:
            continue
        if e.startswith("@"):
            if sender.endswith(e):
                return entry
        elif sender == e:
            return entry
    return None


_MSGID_RE = re.compile(r"<[^<>]+>")


def _thread_length(email: Email) -> int:
    """Estimate the number of messages in the thread from mail headers.

    Uses the count of References ids (+1 for the current message); falls back
    to In-Reply-To (implying at least 2); otherwise 1. Header lookup is
    case-insensitive because mbox headers preserve their original casing.
    """
    headers = {k.lower(): v for k, v in (email.headers or {}).items()}
    refs = headers.get("references")
    if refs:
        ids = _MSGID_RE.findall(refs)
        if ids:
            return len(ids) + 1
    if headers.get("in-reply-to"):
        return 2
    return 1
