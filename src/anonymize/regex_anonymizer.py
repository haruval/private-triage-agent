"""Pattern-based PII detector and anonymizer.

Catches the easy stuff — emails, phones, URLs, dollar amounts, dates,
addresses, credit cards, SSNs. Names and free-form organization references
are out of scope here; those need an NER pass.

Placeholders read as proper nouns to downstream LLMs::

    Email_E1   Phone_F1   Link_U1     Amount_M1
    Date_D1    Address_A1 Card_C1     Ident_S1

Numbering is sequential within one ``anonymize()`` call, and the same
literal value gets the same placeholder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Detection:
    start: int
    end: int
    type: str
    value: str


# ---------------------------------------------------------------------------
# Patterns
#
# Order matters: when two patterns overlap, the one listed FIRST in
# ``_PATTERNS`` wins (after length-prefer logic — see _scan()).
# ---------------------------------------------------------------------------


_URL = re.compile(
    r"""
    (?<![\w/@])
    (?:https?://|www\.)
    [^\s<>"')\],]+
    """,
    re.IGNORECASE | re.VERBOSE,
)

_EMAIL = re.compile(
    r"""
    (?<![\w@])
    [a-zA-Z0-9][a-zA-Z0-9._%+-]*
    @
    [a-zA-Z0-9.-]+
    \.[a-zA-Z]{2,}
    (?![\w@])
    """,
    re.VERBOSE,
)

# US SSN: 3-2-4 with literal dashes.
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")

# Credit-card-like: 4 groups of 4 digits, separated by space or dash.
_CREDIT_CARD = re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)")

# Phone — requires explicit grouping (parens, dashes, dots, or spaces between
# all three groups, OR a leading "+" country code). Bare 10-digit runs are
# intentionally NOT matched to avoid colliding with order numbers, zips, etc.
_PHONE = re.compile(
    r"""
    (?<!\w)
    (?:\+\d{1,3}[\s.-]?)?                  # optional +1 country code
    (?:
        \(\d{3}\)\s?\d{3}[\s.-]?\d{4}      # (555) 123-4567
      | \d{3}[\s.-]\d{3}[\s.-]\d{4}        # 555-123-4567 / 555.123.4567 / 555 123 4567
      | \d{2,4}[\s.-]\d{3,4}[\s.-]\d{3,4}  # international: +44 20 7946 0958
    )
    (?!\w)
    """,
    re.VERBOSE,
)

# Dollar amounts: $1,500.00 / $50,000 / $1.2M / $500
_MONEY = re.compile(
    r"""
    \$\s?\d{1,3}(?:,\d{3})*(?:\.\d+)?[KMBkmb]?
    (?!\w)
    """,
    re.VERBOSE,
)

_DATE = re.compile(
    r"""
    \b(?:
        \d{4}[-/]\d{1,2}[-/]\d{1,2}                                # 2024-01-15
      | \d{1,2}[-/]\d{1,2}[-/]\d{2,4}                              # 1/15/2024
      | (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?
        \s+\d{1,2}(?:,\s*\d{2,4})?                                 # January 15, 2024
      | \d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?
        (?:,?\s*\d{2,4})?                                          # 15 January 2024
    )\b
    """,
    re.VERBOSE,
)

# Address — best-effort. Looks for "<number> <Capitalized words> <street-type>"
# followed by optional apartment / city / state / zip suffix.
_ADDRESS = re.compile(
    r"""
    \b\d+\s+
    (?:
        (?:[A-Z][\w'.-]*|\d+(?:st|nd|rd|th)?\w*)\s+
    ){1,6}
    (?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln
       |Way|Place|Pl|Court|Ct|Parkway|Pkwy|Highway|Hwy)\b\.?
    (?:,\s*(?:Apt|Apartment|Suite|Ste|Unit|\#)\s*[\w-]+)?
    (?:,\s*[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)?
    (?:,?\s*[A-Z]{2})?
    (?:\s+\d{5}(?:-\d{4})?)?
    """,
    re.VERBOSE,
)


# Each row: (type_name, placeholder_prefix, placeholder_letter, compiled_regex)
_PATTERNS: list[tuple[str, str, str, re.Pattern[str]]] = [
    ("url",         "Link",    "U", _URL),
    ("email",       "Email",   "E", _EMAIL),
    ("ssn",         "Ident",   "S", _SSN),
    ("credit_card", "Card",    "C", _CREDIT_CARD),
    ("phone",       "Phone",   "F", _PHONE),
    ("money",       "Amount",  "M", _MONEY),
    ("date",        "Date",    "D", _DATE),
    ("address",     "Address", "A", _ADDRESS),
]

_PRIORITY = {row[0]: i for i, row in enumerate(_PATTERNS)}


# ---------------------------------------------------------------------------
# Anonymizer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawHit:
    start: int
    end: int
    type: str
    prefix: str
    letter: str
    value: str


class RegexAnonymizer:
    """Detect and replace pattern-based PII in plain text.

    Usage::

        a = RegexAnonymizer()
        anonymized, mapping = a.anonymize("Email alice@example.com")
        # → ("Email Email_E1", {"Email_E1": "alice@example.com"})
    """

    # --- public API -------------------------------------------------------

    def detect(self, text: str) -> list[Detection]:
        """Return non-overlapping detections in source order."""
        hits = self._scan(text)
        return [Detection(h.start, h.end, h.type, h.value) for h in hits]

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        hits = self._scan(text)

        # Assign placeholders left-to-right. Same value reuses prior placeholder.
        mapping: dict[str, str] = {}
        value_to_placeholder: dict[str, str] = {}
        counters: dict[str, int] = {}
        for h in hits:
            if h.value in value_to_placeholder:
                continue
            counters[h.letter] = counters.get(h.letter, 0) + 1
            placeholder = f"{h.prefix}_{h.letter}{counters[h.letter]}"
            value_to_placeholder[h.value] = placeholder
            mapping[placeholder] = h.value

        # Apply replacements right-to-left so indices remain valid.
        out = text
        for h in sorted(hits, key=lambda r: r.start, reverse=True):
            placeholder = value_to_placeholder[h.value]
            out = out[: h.start] + placeholder + out[h.end :]

        return out, mapping

    # --- internals --------------------------------------------------------

    def _scan(self, text: str) -> list[_RawHit]:
        raw: list[_RawHit] = []
        for type_name, prefix, letter, pat in _PATTERNS:
            for m in pat.finditer(text):
                value = m.group(0)
                if not value:
                    continue
                raw.append(_RawHit(m.start(), m.end(), type_name, prefix, letter, value))

        # Sort: earliest start ascending, longest span first on tie, then
        # higher-priority type first on still-tie.
        raw.sort(
            key=lambda r: (r.start, -(r.end - r.start), _PRIORITY[r.type])
        )

        chosen: list[_RawHit] = []
        claimed: list[tuple[int, int]] = []
        for r in raw:
            if any(r.start < ce and cs < r.end for cs, ce in claimed):
                continue
            chosen.append(r)
            claimed.append((r.start, r.end))

        chosen.sort(key=lambda r: r.start)
        return chosen
