"""Tests for src/anonymize/rehydrate.py.

Covers each of the cases called out in CLAUDE.md for the rehydration prompt:

- Plain placeholder substitution.
- Possessives, with both straight and curly apostrophes.
- Placeholders adjacent to punctuation (parens, commas, brackets).
- Paraphrased context ("the person Alex_P1 …").
- Unknown placeholders from Claude — left as-is, warning logged.
- Round-trip ``rehydrate(anonymize(text)) == text`` for the regex
  anonymizer, including over the eval corpus.
"""

from __future__ import annotations

import logging

from src.anonymize.regex_anonymizer import RegexAnonymizer
from src.anonymize.rehydrate import rehydrate
from src.eval.corpus import load_corpus


# ---------------------------------------------------------------------------
# Basic substitution
# ---------------------------------------------------------------------------


def test_single_placeholder() -> None:
    assert rehydrate("Ping Alex_P1.", {"Alex_P1": "Sarah"}) == "Ping Sarah."


def test_multiple_distinct_placeholders() -> None:
    text = "Alex_P1 met Bob_P2 at Acme_O1."
    mapping = {"Alex_P1": "Sarah", "Bob_P2": "Jim", "Acme_O1": "DataCorp"}
    assert rehydrate(text, mapping) == "Sarah met Jim at DataCorp."


def test_repeated_placeholder_substituted_each_time() -> None:
    out = rehydrate("Alex_P1 said Alex_P1 was busy.", {"Alex_P1": "Sarah"})
    assert out == "Sarah said Sarah was busy."


def test_no_placeholders_returns_text_unchanged() -> None:
    assert rehydrate("nothing to rehydrate", {"Alex_P1": "Sarah"}) == "nothing to rehydrate"


def test_empty_mapping_with_no_placeholders() -> None:
    assert rehydrate("plain text", {}) == "plain text"


# ---------------------------------------------------------------------------
# Possessives
# ---------------------------------------------------------------------------


def test_possessive_straight_apostrophe() -> None:
    out = rehydrate("Alex_P1's email arrived.", {"Alex_P1": "Sarah"})
    assert out == "Sarah's email arrived."


def test_possessive_curly_apostrophe() -> None:
    out = rehydrate("Alex_P1’s email arrived.", {"Alex_P1": "Sarah"})
    assert out == "Sarah’s email arrived."


def test_possessive_does_not_change_non_possessive_s() -> None:
    # "Alex_P1s" (no apostrophe) is NOT a possessive — the placeholder still
    # rehydrates, and the bare ``s`` is left to whatever Claude wrote.
    out = rehydrate("Alex_P1s", {"Alex_P1": "Sarah"})
    assert out == "Sarahs"


# ---------------------------------------------------------------------------
# Punctuation-adjacent placeholders
# ---------------------------------------------------------------------------


def test_placeholder_in_parens() -> None:
    out = rehydrate("Ping (Alex_P1) about this.", {"Alex_P1": "Sarah"})
    assert out == "Ping (Sarah) about this."


def test_placeholder_with_trailing_comma_and_period() -> None:
    out = rehydrate(
        "Loop in Alex_P1, then Bob_P2.",
        {"Alex_P1": "Sarah", "Bob_P2": "Jim"},
    )
    assert out == "Loop in Sarah, then Jim."


def test_placeholder_in_brackets_and_quotes() -> None:
    out = rehydrate(
        '[Alex_P1] "Acme_O1"',
        {"Alex_P1": "Sarah", "Acme_O1": "DataCorp"},
    )
    assert out == '[Sarah] "DataCorp"'


# ---------------------------------------------------------------------------
# Paraphrased context
# ---------------------------------------------------------------------------


def test_paraphrased_placeholder_substituted_normally() -> None:
    out = rehydrate("the person Alex_P1 will follow up", {"Alex_P1": "Sarah"})
    assert out == "the person Sarah will follow up"


def test_paraphrased_with_descriptors_around_placeholder() -> None:
    out = rehydrate(
        "our contact at Acme_O1 (a vendor) said no",
        {"Acme_O1": "DataCorp"},
    )
    assert out == "our contact at DataCorp (a vendor) said no"


# ---------------------------------------------------------------------------
# Unknown placeholders
# ---------------------------------------------------------------------------


def test_unknown_placeholder_left_as_is(caplog) -> None:
    text = "Alex_P1 introduced Bob_P2 to the team."
    mapping = {"Alex_P1": "Sarah"}  # Bob_P2 is something Claude made up
    with caplog.at_level(logging.WARNING, logger="src.anonymize.rehydrate"):
        out = rehydrate(text, mapping)
    assert out == "Sarah introduced Bob_P2 to the team."
    assert any("Bob_P2" in r.getMessage() for r in caplog.records)


def test_unknown_placeholder_does_not_raise() -> None:
    # Should never raise on a missing key — Claude paraphrasing must not
    # crash the pipeline.
    rehydrate("only Mystery_X9 here", {})


def test_unknown_placeholder_warned_once_per_value(caplog) -> None:
    # Two occurrences of the same unknown placeholder → one warning.
    with caplog.at_level(logging.WARNING, logger="src.anonymize.rehydrate"):
        rehydrate("Ghost_Z1 saw Ghost_Z1 again", {})
    warnings = [r for r in caplog.records if "Ghost_Z1" in r.getMessage()]
    assert len(warnings) == 1


def test_mix_of_known_and_unknown(caplog) -> None:
    text = "Alex_P1 chatted with Ghost_Z9 yesterday."
    with caplog.at_level(logging.WARNING, logger="src.anonymize.rehydrate"):
        out = rehydrate(text, {"Alex_P1": "Sarah"})
    assert out == "Sarah chatted with Ghost_Z9 yesterday."


# ---------------------------------------------------------------------------
# Round-trip against the regex anonymizer
# ---------------------------------------------------------------------------


def test_round_trip_simple() -> None:
    text = "Wire $1,500 to alice@example.com by 2024-12-01."
    anon, mapping = RegexAnonymizer().anonymize(text)
    assert rehydrate(anon, mapping) == text


def test_round_trip_repeated_value() -> None:
    text = "Reply to alice@example.com — alice@example.com is the right one."
    anon, mapping = RegexAnonymizer().anonymize(text)
    assert rehydrate(anon, mapping) == text


def test_round_trip_over_eval_corpus() -> None:
    """For each labeled email, anonymize → rehydrate should be a no-op."""
    a = RegexAnonymizer()
    for example in load_corpus():
        anon, mapping = a.anonymize(example.text)
        assert rehydrate(anon, mapping) == example.text, (
            f"round-trip failed for {example.email_id}"
        )
