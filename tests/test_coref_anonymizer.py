"""Tests for CorefAnonymizer's base-layer fidelity and pronoun resolution.

The coref model itself is never loaded: a stub stands in for fastcoref (the
constructor takes an injected ``coref``), so these tests run offline. The
deterministic tests also inject a fake NER layer; the corpus equivalence
test uses the real spaCy model (session-scoped, skipped if not installed).

Regression background: the base replacements used to be rebuilt from
``CombinedAnonymizer.detect()``'s value re-search, which (a) anchored a
repeated value to the wrong occurrence — garbling text and leaking the real
entity — and (b) silently dropped NER spans that contained a regex
placeholder, leaking their un-replaced remainder. Both leaked raw PII on
the default ``combined`` path.
"""

from __future__ import annotations

import re

import pytest

from src.anonymize.coref_anonymizer import CorefAnonymizer
from src.anonymize.ner_anonymizer import CombinedAnonymizer, NERAnonymizer
from src.anonymize.regex_anonymizer import Detection, RegexAnonymizer
from src.anonymize.rehydrate import rehydrate
from src.eval.corpus import load_corpus


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeNER:
    """Deterministic stand-in for NERAnonymizer: tags fixed patterns."""

    def __init__(self, patterns: list[tuple[str, str]]) -> None:
        self._patterns = [(re.compile(p), t) for p, t in patterns]

    def detect(self, text: str) -> list[Detection]:
        hits = [
            Detection(m.start(), m.end(), type_, m.group(0))
            for pattern, type_ in self._patterns
            for m in pattern.finditer(text)
        ]
        return sorted(hits, key=lambda d: d.start)


class _StubCoref:
    """Stands in for fastcoref's FCoref: returns fixed mention clusters."""

    def __init__(self, clusters: list[list[tuple[int, int]]] | None = None) -> None:
        self._clusters = clusters or []

    def predict(self, texts: list[str]) -> list["_StubResult"]:
        return [_StubResult(self._clusters) for _ in texts]


class _StubResult:
    def __init__(self, clusters: list[list[tuple[int, int]]]) -> None:
        self._clusters = clusters

    def get_clusters(self, as_strings: bool = False) -> list[list[tuple[int, int]]]:
        assert as_strings is False
        return self._clusters


def _coref_over(
    patterns: list[tuple[str, str]],
    clusters: list[list[tuple[int, int]]] | None = None,
) -> CorefAnonymizer:
    base = CombinedAnonymizer(regex=RegexAnonymizer(), ner=_FakeNER(patterns))
    return CorefAnonymizer(base=base, coref=_StubCoref(clusters))


# ---------------------------------------------------------------------------
# Deterministic regressions (fake NER, no models)
# ---------------------------------------------------------------------------


def test_base_span_anchors_to_true_occurrence_not_first_substring() -> None:
    """Regression: 'Ann' must be replaced where NER found it, not inside the
    earlier 'Announcement' — the old value re-search garbled the text and
    left the real name for Claude to see."""
    text = "Announcement: contact Ann today."
    anon = _coref_over([(r"\bAnn\b", "person")])

    out, mapping = anon.anonymize(text)

    assert out == "Announcement: contact Alex_P1 today."
    assert mapping == {"Alex_P1": "Ann"}
    assert rehydrate(out, mapping) == text


def test_ner_span_containing_regex_placeholder_stays_covered() -> None:
    """Regression: an NER span that swallows a regex placeholder used to be
    dropped on the coref path, leaking 'million USD' and keeping a phantom
    mapping entry."""
    text = "We agreed on $2.5 million USD payable to John Smith."
    anon = _coref_over(
        [(r"Amount_M\d+ million USD", "money"), (r"John Smith", "person")]
    )

    out, mapping = anon.anonymize(text)

    assert out == "We agreed on Amount_M1 payable to Alex_P1."
    assert mapping == {"Amount_M1": "$2.5 million USD", "Alex_P1": "John Smith"}
    assert "million" not in out
    # No phantom entries: every placeholder in the mapping was applied.
    assert all(placeholder in out for placeholder in mapping)
    assert rehydrate(out, mapping) == text


def test_pronoun_inherits_entity_placeholder() -> None:
    text = "Ann wrote the draft. She filed it."
    anon = _coref_over([(r"\bAnn\b", "person")], clusters=[[(0, 3), (21, 24)]])

    out, mapping = anon.anonymize(text)

    assert out == "Alex_P1 wrote the draft. Alex_P1 filed it."
    assert mapping == {"Alex_P1": "Ann"}

    detections = {(d.type, d.value) for d in anon.detect(text)}
    assert ("pronoun", "She") in detections


# ---------------------------------------------------------------------------
# Corpus equivalence (real spaCy model)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_combined() -> CombinedAnonymizer:
    try:
        return CombinedAnonymizer(ner=NERAnonymizer())
    except RuntimeError as e:
        pytest.skip(str(e))


def test_no_cluster_coref_matches_base_on_corpus(
    real_combined: CombinedAnonymizer,
) -> None:
    """With zero coref clusters, the coref path must reproduce the base
    anonymization exactly — same text, same mapping, no phantom entries —
    on every example of the hand-labeled corpus."""
    anon = CorefAnonymizer(base=real_combined, coref=_StubCoref())
    for ex in load_corpus():
        base_out, base_map = real_combined.anonymize(ex.text)
        out, mapping = anon.anonymize(ex.text)
        assert (out, mapping) == (base_out, base_map), f"diverged on {ex.text!r}"
        assert all(placeholder in out for placeholder in mapping)
