"""Detects whether sensitive values survived anonymization.

Given the original text, the anonymized text, and the mapping the anonymizer
used (placeholder -> sensitive value), check whether any of those sensitive
values still appear in the anonymized output. Matches are case-insensitive
and word-boundary aware, so "Mark" does not match "marketing" but
"alice@example.com" matches itself regardless of surrounding whitespace or
punctuation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class LeakReport:
    leaked_tokens: list[str]
    leak_count: int
    leak_positions: list[tuple[int, int]]


def detect_leaks(
    original: str,
    anonymized: str,
    mapping: dict[str, str],
) -> LeakReport:
    """Scan ``anonymized`` for any value from ``mapping.values()``.

    Longer values are matched first so overlapping hits (e.g. "Robert" inside
    "Robert Smith") are counted once, against the longest matching value.
    """
    del original  # accepted for API symmetry; not currently consulted

    leaked_tokens: list[str] = []
    leak_positions: list[tuple[int, int]] = []
    covered: list[tuple[int, int]] = []

    unique_values = sorted(
        {v for v in mapping.values() if v},
        key=lambda v: (-len(v), v),
    )

    for value in unique_values:
        pattern = _word_bounded_pattern(value)
        for m in re.finditer(pattern, anonymized, flags=re.IGNORECASE):
            start, end = m.start(), m.end()
            if _overlaps_any(start, end, covered):
                continue
            covered.append((start, end))
            leaked_tokens.append(value)
            leak_positions.append((start, end))

    order = sorted(range(len(leak_positions)), key=lambda i: leak_positions[i])
    leaked_tokens = [leaked_tokens[i] for i in order]
    leak_positions = [leak_positions[i] for i in order]

    return LeakReport(
        leaked_tokens=leaked_tokens,
        leak_count=len(leaked_tokens),
        leak_positions=leak_positions,
    )


def _word_bounded_pattern(value: str) -> str:
    """Wrap ``value`` in lookarounds that act like a word boundary.

    Standard ``\\b`` is unreliable when the value starts or ends with
    non-word characters (e.g. ``$1,500.00``). We only apply the boundary on
    sides whose terminal char is alphanumeric / underscore.
    """
    escaped = re.escape(value)
    left = r"(?<!\w)" if _is_word_char(value[:1]) else ""
    right = r"(?!\w)" if _is_word_char(value[-1:]) else ""
    return f"{left}{escaped}{right}"


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch == "_")


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for cs, ce in spans:
        if start < ce and cs < end:
            return True
    return False
