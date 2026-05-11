"""Tests for src/anonymize/regex_anonymizer.py.

Two layers:

1. Unit tests for each PII type and the placeholder / mapping contract.
2. End-to-end precision & recall on the labeled eval corpus, printed as a
   table. Run with ``pytest -s`` to see the report (or read it from a
   failure trace). Recall and precision are asserted against thresholds at
   the bottom so regressions trip the build.
"""

from __future__ import annotations

import pytest

from src.anonymize.regex_anonymizer import Detection, RegexAnonymizer
from src.eval.corpus import load_corpus

# Types the regex anonymizer is responsible for. Names, companies,
# project codenames, and pronouns require an NER pass and are excluded
# from regex precision/recall.
REGEX_TYPES: frozenset[str] = frozenset(
    {"email", "phone", "money", "address", "date", "url", "credit_card", "ssn"}
)


# ---------------------------------------------------------------------------
# Per-type detection / placeholder unit tests
# ---------------------------------------------------------------------------


def test_email_detected_and_replaced() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("Email me at alice@example.com please.")
    assert "alice@example.com" not in out
    assert "Email_E1" in out
    assert mapping == {"Email_E1": "alice@example.com"}


def test_phone_us_paren_format() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("Call (555) 123-4567 anytime.")
    assert "(555) 123-4567" not in out
    assert mapping == {"Phone_F1": "(555) 123-4567"}


def test_phone_dashed_us_format() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("My cell is 415-555-2020.")
    assert mapping == {"Phone_F1": "415-555-2020"}
    assert "415-555-2020" not in out


def test_phone_international_with_country_code() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("UK office: +44 20 7946 0958, call after 9am.")
    assert "+44 20 7946 0958" not in out
    assert any(v == "+44 20 7946 0958" for v in mapping.values())


def test_url_detected() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("See https://example.com/foo?x=1 for details.")
    assert "https://example.com/foo?x=1" not in out
    assert mapping == {"Link_U1": "https://example.com/foo?x=1"}


def test_money_variants() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("Q2: $1.2M, Q3: $50,000, Q4: $1,500.00.")
    # All three replaced with sequentially numbered Amount_Mn placeholders.
    assert "$1.2M" not in out
    assert "$50,000" not in out
    assert "$1,500.00" not in out
    assert mapping == {
        "Amount_M1": "$1.2M",
        "Amount_M2": "$50,000",
        "Amount_M3": "$1,500.00",
    }


def test_ssn_detected() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("SSN on file: 123-45-6789, do not share.")
    assert "123-45-6789" not in out
    assert mapping == {"Ident_S1": "123-45-6789"}


def test_credit_card_detected() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("Card 4111-1111-1111-1111 declined.")
    assert "4111-1111-1111-1111" not in out
    assert mapping == {"Card_C1": "4111-1111-1111-1111"}


def test_date_iso_and_word_formats() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize("Filed 2024-01-15. Reviewed January 22, 2024.")
    # Both dates detected; placeholders sequential.
    assert "2024-01-15" not in out
    assert "January 22, 2024" not in out
    assert set(mapping.values()) == {"2024-01-15", "January 22, 2024"}
    assert "Date_D1" in out and "Date_D2" in out


def test_address_with_city_state_zip() -> None:
    a = RegexAnonymizer()
    text = "Ship to 1600 Pennsylvania Ave, Washington DC by Friday."
    out, mapping = a.anonymize(text)
    assert "1600 Pennsylvania Ave, Washington DC" not in out
    assert mapping == {"Address_A1": "1600 Pennsylvania Ave, Washington DC"}


# ---------------------------------------------------------------------------
# Mapping / placeholder contract
# ---------------------------------------------------------------------------


def test_same_value_reuses_placeholder() -> None:
    a = RegexAnonymizer()
    out, mapping = a.anonymize(
        "Email alice@example.com, then alice@example.com again."
    )
    assert out.count("Email_E1") == 2
    assert "Email_E2" not in out
    assert mapping == {"Email_E1": "alice@example.com"}


def test_per_type_counter_is_independent() -> None:
    """Each type has its own counter — Phone_F1 and Email_E1 coexist."""
    a = RegexAnonymizer()
    _, mapping = a.anonymize(
        "Reach me at alice@example.com or (555) 123-4567, "
        "or bob@example.com / 415-555-2020."
    )
    assert mapping == {
        "Email_E1": "alice@example.com",
        "Phone_F1": "(555) 123-4567",
        "Email_E2": "bob@example.com",
        "Phone_F2": "415-555-2020",
    }


def test_no_false_positive_on_clean_text() -> None:
    """'Mark on the marketing team' must not produce any anonymization."""
    a = RegexAnonymizer()
    text = "Mark is on the marketing team and reports to Jennifer."
    out, mapping = a.anonymize(text)
    assert out == text
    assert mapping == {}


def test_anonymized_text_is_round_trip_recoverable() -> None:
    """Substituting placeholders back with mapping values restores original."""
    a = RegexAnonymizer()
    text = (
        "Send the $50,000 wire to alice@example.com today; "
        "her cell is (555) 123-4567."
    )
    out, mapping = a.anonymize(text)
    restored = out
    for placeholder, value in mapping.items():
        restored = restored.replace(placeholder, value)
    assert restored == text


def test_detect_returns_spans_with_correct_offsets() -> None:
    a = RegexAnonymizer()
    text = "Email alice@example.com today."
    dets = a.detect(text)
    assert len(dets) == 1
    d = dets[0]
    assert isinstance(d, Detection)
    assert d.type == "email"
    assert text[d.start:d.end] == d.value == "alice@example.com"


# ---------------------------------------------------------------------------
# Corpus precision/recall
# ---------------------------------------------------------------------------


def _score_corpus() -> tuple[dict[str, dict[str, int]], list[tuple[str, str, str]]]:
    """Return (per-type counts, list of (email_id, kind, detail) for misses).

    ``kind`` is "fp" or "fn".
    """
    a = RegexAnonymizer()
    examples = load_corpus()

    per_type: dict[str, dict[str, int]] = {}
    notes: list[tuple[str, str, str]] = []

    def bump(t: str, key: str) -> None:
        per_type.setdefault(t, {"tp": 0, "fp": 0, "fn": 0})[key] += 1

    for ex in examples:
        gold = [s for s in ex.sensitive_spans if s.type in REGEX_TYPES]
        pred = a.detect(ex.text)

        used_gold: set[int] = set()
        used_pred: set[int] = set()

        # Match by any overlap; type is taken from the gold label.
        for gi, g in enumerate(gold):
            for pi, p in enumerate(pred):
                if pi in used_pred:
                    continue
                if g.start < p.end and p.start < g.end:
                    bump(g.type, "tp")
                    used_gold.add(gi)
                    used_pred.add(pi)
                    break

        for gi, g in enumerate(gold):
            if gi not in used_gold:
                bump(g.type, "fn")
                notes.append((ex.email_id, "fn", f"{g.type}: {g.value!r}"))
        for pi, p in enumerate(pred):
            if pi not in used_pred:
                bump(p.type, "fp")
                notes.append((ex.email_id, "fp", f"{p.type}: {p.value!r}"))

    return per_type, notes


def _format_report(per_type: dict[str, dict[str, int]]) -> str:
    lines = []
    lines.append("\n=== RegexAnonymizer precision/recall on eval corpus ===")
    lines.append(
        f"{'type':>12}  {'precision':>9}  {'recall':>6}  "
        f"{'TP':>3} {'FP':>3} {'FN':>3}"
    )
    total_tp = total_fp = total_fn = 0
    for t in sorted(per_type):
        tp = per_type[t]["tp"]
        fp = per_type[t]["fp"]
        fn = per_type[t]["fn"]
        total_tp += tp
        total_fp += fp
        total_fn += fn
        prec = tp / (tp + fp) if (tp + fp) else 1.0
        rec = tp / (tp + fn) if (tp + fn) else 1.0
        lines.append(
            f"{t:>12}  {prec:>9.2f}  {rec:>6.2f}  {tp:>3} {fp:>3} {fn:>3}"
        )
    op = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    orec = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0
    lines.append(
        f"{'OVERALL':>12}  {op:>9.2f}  {orec:>6.2f}  "
        f"{total_tp:>3} {total_fp:>3} {total_fn:>3}"
    )
    return "\n".join(lines)


def test_corpus_precision_recall_meets_thresholds(capsys) -> None:
    """End-to-end metrics on the labeled corpus.

    Run with ``pytest -s tests/test_regex_anonymizer.py`` to see the table.
    """
    per_type, notes = _score_corpus()
    report = _format_report(per_type)

    # Print uncaptured so the report always shows in the test output.
    with capsys.disabled():
        print(report)
        if notes:
            print("\nmisses:")
            for email_id, kind, detail in notes:
                print(f"  {email_id}  {kind}  {detail}")
        print()

    # Overall thresholds. Regex alone should be near-perfect on the
    # patterns it's responsible for; names/companies/etc. are excluded.
    total_tp = sum(v["tp"] for v in per_type.values())
    total_fp = sum(v["fp"] for v in per_type.values())
    total_fn = sum(v["fn"] for v in per_type.values())

    prec = total_tp / (total_tp + total_fp) if (total_tp + total_fp) else 1.0
    rec = total_tp / (total_tp + total_fn) if (total_tp + total_fn) else 1.0

    assert rec >= 0.9, f"recall {rec:.2f} below 0.9 — missed regex-detectable PII"
    assert prec >= 0.9, f"precision {prec:.2f} below 0.9 — too many false positives"


@pytest.mark.parametrize("type_", sorted(["email", "phone", "money", "address"]))
def test_corpus_per_type_recall_is_perfect(type_: str) -> None:
    """For every regex type present in the corpus, recall should be 1.0.

    If this breaks, look at the misses printed by the overall test.
    """
    per_type, _ = _score_corpus()
    counts = per_type.get(type_)
    assert counts is not None, f"no {type_} spans found in corpus"
    rec = counts["tp"] / (counts["tp"] + counts["fn"])
    assert rec == 1.0, f"{type_} recall {rec:.2f}, expected 1.0"
