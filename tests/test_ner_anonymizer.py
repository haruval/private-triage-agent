"""Tests for NERAnonymizer + CombinedAnonymizer, plus a cross-strategy
precision/recall/F1 report on the eval corpus.

The spaCy ``en_core_web_trf`` model is heavy to load (~5-10s), so it's
session-scoped. If the model isn't installed, the NER-dependent tests skip.

Run with ``pytest -s tests/test_ner_anonymizer.py`` to see the comparison
table; it's also printed via ``capsys.disabled()`` so it shows even when
the assertions pass.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.anonymize.ner_anonymizer import (
    CombinedAnonymizer,
    NERAnonymizer,
    _RawHit,
    _resolve_overlaps,
)
from src.anonymize.regex_anonymizer import Detection, RegexAnonymizer
from src.eval.corpus import EvalExample, load_corpus
from src.eval.leak_detector import detect_leaks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ner_anonymizer() -> NERAnonymizer:
    try:
        return NERAnonymizer()
    except RuntimeError as e:
        pytest.skip(str(e))


@pytest.fixture(scope="session")
def combined_anonymizer(ner_anonymizer: NERAnonymizer) -> CombinedAnonymizer:
    return CombinedAnonymizer(ner=ner_anonymizer)


# ---------------------------------------------------------------------------
# Pure-logic tests (no spaCy required)
# ---------------------------------------------------------------------------


def _hit(start: int, end: int, value: str, type_: str = "person") -> _RawHit:
    return _RawHit(
        start=start, end=end, type=type_, prefix="Alex", letter="P", value=value
    )


def test_resolve_overlaps_prefers_longer_span() -> None:
    """A short hit inside a longer one is dropped."""
    short = _hit(0, 5, "Robert")
    long_ = _hit(0, 12, "Robert Smith")
    chosen = _resolve_overlaps([short, long_])
    assert chosen == [long_]


def test_resolve_overlaps_keeps_disjoint_hits() -> None:
    a = _hit(0, 5, "Alice")
    b = _hit(10, 13, "Bob")
    chosen = _resolve_overlaps([a, b])
    assert chosen == [a, b]


def test_resolve_overlaps_drops_partial_overlap_after_longer() -> None:
    """0-12 and 8-20 overlap — keep the first (it was longer)."""
    long_ = _hit(0, 12, "Robert Smith")
    other = _hit(8, 20, "Smith called")
    chosen = _resolve_overlaps([long_, other])
    assert chosen == [long_]


def test_resolve_overlaps_returns_in_source_order() -> None:
    b = _hit(10, 13, "Bob")
    a = _hit(0, 5, "Alice")
    chosen = _resolve_overlaps([b, a])
    assert [h.start for h in chosen] == [0, 10]


# ---------------------------------------------------------------------------
# NERAnonymizer with real spaCy
# ---------------------------------------------------------------------------


def test_ner_detects_person_and_org(ner_anonymizer: NERAnonymizer) -> None:
    dets = ner_anonymizer.detect("Alice works at Acme Corp on the platform team.")
    types = {(d.type, d.value) for d in dets}
    assert ("person", "Alice") in types
    assert ("org", "Acme Corp") in types


def test_ner_placeholder_format(ner_anonymizer: NERAnonymizer) -> None:
    text = "Alice met Bob at Acme yesterday."
    out, mapping = ner_anonymizer.anonymize(text)

    # No original sensitive value survives in the output as a standalone token.
    # (We use the leak detector because placeholder stems like "Alex" / "Acme"
    # would trip naive substring checks but aren't actual leaks.)
    leak = detect_leaks(text, out, mapping)
    assert leak.leak_count == 0, f"leaks: {leak.leaked_tokens} in {out!r}"

    # Placeholders follow the proper-noun convention.
    persons = [k for k in mapping if k.startswith("Alex_P")]
    orgs = [k for k in mapping if k.startswith("Acme_O")]
    dates = [k for k in mapping if k.startswith("Date_D")]
    assert len(persons) == 2, f"expected 2 person placeholders, got {persons}"
    assert len(orgs) == 1
    assert len(dates) == 1


def test_ner_same_string_same_placeholder(ner_anonymizer: NERAnonymizer) -> None:
    """Two occurrences of the same name share one placeholder."""
    out, mapping = ner_anonymizer.anonymize(
        "Alice called Bob. Later, Alice emailed Bob again."
    )
    # Each placeholder appears twice in the output.
    assert mapping  # at least one detection
    for placeholder, value in mapping.items():
        if value in {"Alice", "Bob"}:
            assert out.count(placeholder) == 2, (
                f"{value} → {placeholder} appeared "
                f"{out.count(placeholder)}× in {out!r}"
            )


# ---------------------------------------------------------------------------
# CombinedAnonymizer
# ---------------------------------------------------------------------------


def test_combined_catches_both_regex_and_ner_in_one_email(
    combined_anonymizer: CombinedAnonymizer,
) -> None:
    text = "Lisa Chen (lisa@acme.io) at Acme handled the $50,000 retainer."
    out, mapping = combined_anonymizer.anonymize(text)

    # No original sensitive token survives. Substring check is unsafe here
    # because placeholder stems ("Alex", "Acme") look like real tokens.
    leak = detect_leaks(text, out, mapping)
    assert leak.leak_count == 0, f"leaks: {leak.leaked_tokens} in {out!r}"

    # Mapping contains placeholders from BOTH passes.
    letters = {k.split("_")[1][0] for k in mapping}
    assert "E" in letters, f"missing email placeholder in {mapping}"
    assert "P" in letters, f"missing person placeholder in {mapping}"
    assert "O" in letters, f"missing org placeholder in {mapping}"
    assert "M" in letters, f"missing money placeholder in {mapping}"


def test_combined_regex_placeholders_pass_through(
    combined_anonymizer: CombinedAnonymizer,
) -> None:
    """Regex-replaced spans should not be re-tagged by NER."""
    text = "Email alice@example.com about the $500 invoice."
    _, mapping = combined_anonymizer.anonymize(text)
    # The email and money values should appear as values in the mapping,
    # not split into person-token segments.
    assert "alice@example.com" in mapping.values()
    assert "$500" in mapping.values()


def test_combined_same_type_placeholders_do_not_collide_and_round_trip(
    combined_anonymizer: CombinedAnonymizer,
) -> None:
    """Regression: regex and NER share the date letter 'D' (and money 'M').

    The regex pass tags the ISO date ``2025-03-14`` as ``Date_D1``; the NER
    pass independently tags the weekday ``Friday`` as a DATE. Before the fix
    both passes numbered from 1, so the NER ``Date_D1`` collided with the
    regex ``Date_D1`` and the mapping merge dropped one — rehydration then
    restored the wrong value. The passes must produce globally-unique
    placeholders so the round-trip is exact.
    """
    from src.anonymize.rehydrate import rehydrate

    text = "The deadline moved from 2025-03-14 to Friday; please confirm."
    out, mapping = combined_anonymizer.anonymize(text)

    # Both date values survive in the mapping, under distinct keys.
    assert "2025-03-14" in mapping.values()
    assert "Friday" in mapping.values()
    assert len(set(mapping)) == len(mapping)  # no key collision
    # Two date placeholders, both present in the output.
    date_keys = [k for k in mapping if k.startswith("Date_D")]
    assert len(date_keys) == 2, f"expected 2 date placeholders, got {date_keys}"
    assert all(out.count(k) == 1 for k in date_keys)

    # The merge is lossless and rehydration restores the original exactly.
    assert rehydrate(out, mapping) == text


def test_combined_detect_offsets_are_in_original_text(
    combined_anonymizer: CombinedAnonymizer,
) -> None:
    text = "Daniel at Initech (daniel.chen@initech.com)."
    dets = combined_anonymizer.detect(text)
    # Every detection's substring of the original should equal its value.
    for d in dets:
        assert text[d.start:d.end] == d.value, (
            f"offset mismatch for {d}: text slice is {text[d.start:d.end]!r}"
        )


# ---------------------------------------------------------------------------
# Cross-strategy precision/recall/F1 on the eval corpus
# ---------------------------------------------------------------------------


@dataclass
class _Metrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _score_example(
    gold: list, pred: list[Detection]
) -> tuple[_Metrics, dict[str, _Metrics]]:
    """Match by any character overlap; count by gold type for the per-type view."""
    overall = _Metrics()
    per_type: dict[str, _Metrics] = {}
    used_pred: set[int] = set()

    for g in gold:
        pt = per_type.setdefault(g.type, _Metrics())
        matched = False
        for pi, p in enumerate(pred):
            if pi in used_pred:
                continue
            if g.start < p.end and p.start < g.end:
                overall.tp += 1
                pt.tp += 1
                used_pred.add(pi)
                matched = True
                break
        if not matched:
            overall.fn += 1
            pt.fn += 1

    for pi, _ in enumerate(pred):
        if pi not in used_pred:
            overall.fp += 1

    return overall, per_type


def _score_corpus(
    examples: list[EvalExample], anon
) -> tuple[_Metrics, dict[str, _Metrics]]:
    overall = _Metrics()
    per_type: dict[str, _Metrics] = {}
    for ex in examples:
        ov, pt = _score_example(ex.sensitive_spans, anon.detect(ex.text))
        overall.tp += ov.tp
        overall.fp += ov.fp
        overall.fn += ov.fn
        for k, v in pt.items():
            m = per_type.setdefault(k, _Metrics())
            m.tp += v.tp
            m.fp += v.fp
            m.fn += v.fn
    return overall, per_type


def _format_comparison(
    results: dict[str, tuple[_Metrics, dict[str, _Metrics]]],
) -> str:
    names = list(results)
    lines: list[str] = []
    lines.append("\n=== Anonymizer strategies vs. eval corpus ===\n")

    # Overall table
    header = f"{'metric':<10}" + "".join(f"{n:>12}" for n in names)
    lines.append(header)
    lines.append("-" * len(header))
    for label, attr in [("precision", "precision"), ("recall", "recall"), ("F1", "f1")]:
        row = f"{label:<10}"
        for n in names:
            v = getattr(results[n][0], attr)
            row += f"{v:>12.2f}"
        lines.append(row)
    for label, attr in [("TP", "tp"), ("FP", "fp"), ("FN", "fn")]:
        row = f"{label:<10}"
        for n in names:
            v = getattr(results[n][0], attr)
            row += f"{v:>12d}"
        lines.append(row)

    # Per-gold-type recall side by side
    lines.append("\nper-gold-type recall:")
    all_types: set[str] = set()
    for _, pt in results.values():
        all_types.update(pt)
    type_hdr = f"  {'gold type':<18}" + "".join(f"{n:>12}" for n in names)
    lines.append(type_hdr)
    lines.append("  " + "-" * (len(type_hdr) - 2))
    for t in sorted(all_types):
        row = f"  {t:<18}"
        for n in names:
            pt = results[n][1]
            m = pt.get(t)
            row += f"{(m.recall if m else 0.0):>12.2f}"
        lines.append(row)

    return "\n".join(lines)


def test_corpus_three_strategy_comparison(
    ner_anonymizer: NERAnonymizer,
    combined_anonymizer: CombinedAnonymizer,
    capsys,
) -> None:
    examples = load_corpus()
    strategies = {
        "regex":    RegexAnonymizer(),
        "ner":      ner_anonymizer,
        "combined": combined_anonymizer,
    }
    results = {name: _score_corpus(examples, anon) for name, anon in strategies.items()}

    report = _format_comparison(results)
    with capsys.disabled():
        print(report)
        print()

    regex_ov   = results["regex"][0]
    ner_ov     = results["ner"][0]
    combined_ov = results["combined"][0]

    # By construction, combined.detect ⊇ regex.detect (combined keeps every
    # regex hit and adds NER hits on top), so combined recall must be at
    # least regex recall AND at least NER-on-original recall is not a
    # guarantee (NER runs on regex-anonymized text), but in practice both
    # hold on this corpus.
    assert combined_ov.recall >= regex_ov.recall - 1e-9, (
        f"combined recall {combined_ov.recall:.2f} below regex {regex_ov.recall:.2f}"
    )
    assert combined_ov.recall >= ner_ov.recall - 1e-9, (
        f"combined recall {combined_ov.recall:.2f} below ner {ner_ov.recall:.2f}"
    )

    # Each strategy should at least be detecting something useful.
    assert regex_ov.f1 > 0.3, f"regex F1 too low: {regex_ov.f1:.2f}"
    assert ner_ov.f1 > 0.3, f"ner F1 too low: {ner_ov.f1:.2f}"
    assert combined_ov.f1 > 0.5, f"combined F1 too low: {combined_ov.f1:.2f}"
