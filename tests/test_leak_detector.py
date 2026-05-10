"""Tests for src/eval/leak_detector.py.

Covers the empty / clean cases, the basic positive case, word-boundary
behavior (the canonical "Mark" vs "marketing" trap), case-insensitivity,
multi-hit counting, overlap collapsing, and non-word-char payloads like
email addresses and dollar amounts.
"""

from __future__ import annotations

from src.eval.leak_detector import LeakReport, detect_leaks


def test_empty_mapping_produces_no_leaks() -> None:
    report = detect_leaks("Hi Bob, see attached.", "Hi Bob, see attached.", {})
    assert isinstance(report, LeakReport)
    assert report.leaked_tokens == []
    assert report.leak_count == 0
    assert report.leak_positions == []


def test_value_fully_replaced_produces_no_leak() -> None:
    original = "Hi Bob, see attached."
    anonymized = "Hi PERSON_001, see attached."
    mapping = {"PERSON_001": "Bob"}
    report = detect_leaks(original, anonymized, mapping)
    assert report.leak_count == 0
    assert report.leaked_tokens == []


def test_value_present_in_anonymized_is_a_leak() -> None:
    original = "Hi Bob, see attached."
    anonymized = "Hi Bob, see attached."  # anonymizer forgot to redact
    mapping = {"PERSON_001": "Bob"}
    report = detect_leaks(original, anonymized, mapping)
    assert report.leak_count == 1
    assert report.leaked_tokens == ["Bob"]
    assert report.leak_positions == [(3, 6)]
    assert anonymized[3:6] == "Bob"


def test_word_boundary_mark_does_not_match_marketing() -> None:
    """The canonical false-positive: 'Mark' as a name must not fire on 'marketing'."""
    original = "Send to Mark on the marketing team."
    anonymized = "Send to PERSON_001 on the marketing team."
    mapping = {"PERSON_001": "Mark"}
    report = detect_leaks(original, anonymized, mapping)
    assert report.leak_count == 0, (
        f"'marketing' should not be flagged as a leak of 'Mark', got {report}"
    )


def test_word_boundary_short_number_inside_longer_number_is_not_a_leak() -> None:
    """'123' inside ID '12345' is not a phone-number leak."""
    anonymized = "Ticket ID 12345 was filed yesterday."
    mapping = {"PHONE_001": "123"}
    report = detect_leaks("(original irrelevant)", anonymized, mapping)
    assert report.leak_count == 0


def test_case_insensitive_match() -> None:
    anonymized = "the alice@example.com address bounced"
    mapping = {"EMAIL_001": "Alice@Example.COM"}
    report = detect_leaks("", anonymized, mapping)
    assert report.leak_count == 1
    assert report.leaked_tokens == ["Alice@Example.COM"]
    s, e = report.leak_positions[0]
    assert anonymized[s:e].lower() == "alice@example.com"


def test_multiple_occurrences_of_same_value_are_each_counted() -> None:
    anonymized = "Bob met Bob in the hallway and Bob waved."
    mapping = {"PERSON_001": "Bob"}
    report = detect_leaks("", anonymized, mapping)
    assert report.leak_count == 3
    assert report.leaked_tokens == ["Bob", "Bob", "Bob"]
    assert report.leak_positions == [(0, 3), (8, 11), (31, 34)]


def test_overlapping_values_count_once_against_longest() -> None:
    """If both 'Robert Smith' and 'Robert' leak at the same spot, count one."""
    anonymized = "Robert Smith called this morning."
    mapping = {"PERSON_001": "Robert Smith", "PERSON_002": "Robert"}
    report = detect_leaks("", anonymized, mapping)
    assert report.leak_count == 1
    assert report.leaked_tokens == ["Robert Smith"]
    assert report.leak_positions == [(0, 12)]


def test_value_with_non_word_chars_matches_when_adjacent_to_word_chars() -> None:
    """'$1,500.00' should match even when not separated by whitespace at the '$' side."""
    anonymized = "Invoice total: $1,500.00 due Friday."
    mapping = {"MONEY_001": "$1,500.00"}
    report = detect_leaks("", anonymized, mapping)
    assert report.leak_count == 1
    assert report.leaked_tokens == ["$1,500.00"]
    s, e = report.leak_positions[0]
    assert anonymized[s:e] == "$1,500.00"


def test_email_value_matches_inside_punctuation() -> None:
    """An email surrounded by parens / commas should still be flagged."""
    anonymized = "Forwarded (daniel.chen@initech.com), please review."
    mapping = {"EMAIL_001": "daniel.chen@initech.com"}
    report = detect_leaks("", anonymized, mapping)
    assert report.leak_count == 1
    s, e = report.leak_positions[0]
    assert anonymized[s:e] == "daniel.chen@initech.com"


def test_positions_are_sorted_left_to_right_across_values() -> None:
    """Even when multiple distinct values leak, positions come back in source order."""
    anonymized = "Acme paid Bob $500 yesterday."
    mapping = {
        "C": "Acme",
        "P": "Bob",
        "M": "$500",
    }
    report = detect_leaks("", anonymized, mapping)
    assert report.leak_count == 3
    assert report.leak_positions == sorted(report.leak_positions)
    # leaked_tokens reorder to follow position order
    assert report.leaked_tokens == ["Acme", "Bob", "$500"]
