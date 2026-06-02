"""NER-based anonymizer backed by spaCy ``en_core_web_trf``.

Catches the things regex can't: free-form person names, organizations,
locations, facilities. Same interface as :class:`RegexAnonymizer`.

Placeholders read as proper nouns to downstream LLMs::

    Alex_P1     person       (PERSON)
    Acme_O1     organization (ORG)
    Westview_G1 geo/location (GPE)
    Beacon_K1   facility     (FAC)
    Amount_M1   money        (MONEY)
    Date_D1     date         (DATE)

A :class:`CombinedAnonymizer` runs regex first, then NER on the
regex-anonymized text — so emails, phones, dollar amounts, etc. don't get
re-tagged (or worse, partially re-tagged) by the NER pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.anonymize.regex_anonymizer import Detection, RegexAnonymizer

# spaCy entity label -> (type_name, placeholder_prefix, placeholder_letter)
_NER_LABELS: dict[str, tuple[str, str, str]] = {
    "PERSON": ("person",   "Alex",     "P"),
    "ORG":    ("org",      "Acme",     "O"),
    "GPE":    ("gpe",      "Westview", "G"),
    "MONEY":  ("money",    "Amount",   "M"),
    "DATE":   ("date",     "Date",     "D"),
    "FAC":    ("facility", "Beacon",   "K"),
}

DEFAULT_MODEL = "en_core_web_trf"


@dataclass(frozen=True)
class _RawHit:
    start: int
    end: int
    type: str
    prefix: str
    letter: str
    value: str


def _resolve_overlaps(hits: list[_RawHit]) -> list[_RawHit]:
    """Drop any hit that overlaps a longer (or equal-and-earlier) hit."""
    hits = sorted(hits, key=lambda h: (h.start, -(h.end - h.start)))
    chosen: list[_RawHit] = []
    claimed: list[tuple[int, int]] = []
    for h in hits:
        if any(h.start < ce and cs < h.end for cs, ce in claimed):
            continue
        chosen.append(h)
        claimed.append((h.start, h.end))
    return sorted(chosen, key=lambda h: h.start)


_PLACEHOLDER_SUFFIX_RE = re.compile(r"_([A-Z])(\d+)$")


def _counts_from_mapping(mapping: dict[str, str]) -> dict[str, int]:
    """Highest placeholder number already used per letter.

    Lets a second pass continue numbering (``Date_D2``, ``Date_D3``, …)
    instead of restarting at 1 and colliding with the first pass's keys.
    """
    counts: dict[str, int] = {}
    for placeholder in mapping:
        m = _PLACEHOLDER_SUFFIX_RE.search(placeholder)
        if not m:
            continue
        letter, num = m.group(1), int(m.group(2))
        counts[letter] = max(counts.get(letter, 0), num)
    return counts


def _apply_placeholders(
    text: str,
    hits: list[_RawHit],
    *,
    start_counters: dict[str, int] | None = None,
) -> tuple[str, dict[str, str]]:
    """Assign sequential placeholders per letter and substitute right-to-left.

    ``start_counters`` seeds the per-letter counters so a second pass picks up
    where an earlier one left off (used by :class:`CombinedAnonymizer` to keep
    NER placeholders from colliding with regex ones on shared letters).
    """
    mapping: dict[str, str] = {}
    value_to_placeholder: dict[str, str] = {}
    counters: dict[str, int] = dict(start_counters) if start_counters else {}
    for h in hits:
        if h.value in value_to_placeholder:
            continue
        counters[h.letter] = counters.get(h.letter, 0) + 1
        placeholder = f"{h.prefix}_{h.letter}{counters[h.letter]}"
        value_to_placeholder[h.value] = placeholder
        mapping[placeholder] = h.value

    out = text
    for h in sorted(hits, key=lambda r: r.start, reverse=True):
        placeholder = value_to_placeholder[h.value]
        out = out[: h.start] + placeholder + out[h.end :]
    return out, mapping


# ---------------------------------------------------------------------------
# NERAnonymizer
# ---------------------------------------------------------------------------


class NERAnonymizer:
    """spaCy-based detector. Public surface mirrors :class:`RegexAnonymizer`.

    The model loads once per instance. For tests / scripts that build many
    anonymizers, reuse a single instance — or pass an already-loaded
    ``nlp`` to the constructor.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        nlp: Any = None,
    ) -> None:
        if nlp is not None:
            self._nlp = nlp
        else:
            import spacy  # imported lazily so unit tests can skip if missing
            try:
                self._nlp = spacy.load(model_name)
            except OSError as e:
                raise RuntimeError(
                    f"spaCy model {model_name!r} not installed. Run:\n"
                    f"  python -m spacy download {model_name}"
                ) from e

    # --- public API -------------------------------------------------------

    def detect(self, text: str) -> list[Detection]:
        hits = self._scan(text)
        return [Detection(h.start, h.end, h.type, h.value) for h in hits]

    def anonymize(
        self, text: str, *, start_counters: dict[str, int] | None = None
    ) -> tuple[str, dict[str, str]]:
        hits = self._scan(text)
        return _apply_placeholders(text, hits, start_counters=start_counters)

    # --- internals --------------------------------------------------------

    def _scan(self, text: str) -> list[_RawHit]:
        doc = self._nlp(text)
        raw: list[_RawHit] = []
        for ent in doc.ents:
            label = ent.label_
            if label not in _NER_LABELS:
                continue
            type_name, prefix, letter = _NER_LABELS[label]
            s, e = ent.start_char, ent.end_char
            value = text[s:e]
            if not value.strip():
                continue
            raw.append(_RawHit(s, e, type_name, prefix, letter, value))
        return _resolve_overlaps(raw)


# ---------------------------------------------------------------------------
# CombinedAnonymizer
# ---------------------------------------------------------------------------


class CombinedAnonymizer:
    """Regex first, then NER on the regex-anonymized text.

    The regex pass replaces structured PII (emails, phones, $ amounts, dates,
    addresses, SSNs, credit cards, URLs) with proper-noun-shaped placeholders
    like ``Email_E1``. spaCy then runs on the substituted text and tags the
    remaining named entities — names, orgs, locations, etc.

    Because regex placeholders don't look like the original tokens, NER won't
    re-tag the values it already replaced.
    """

    def __init__(
        self,
        *,
        regex: RegexAnonymizer | None = None,
        ner: NERAnonymizer | None = None,
        model_name: str = DEFAULT_MODEL,
    ) -> None:
        self._regex = regex if regex is not None else RegexAnonymizer()
        self._ner = ner if ner is not None else NERAnonymizer(model_name=model_name)

    # --- public API -------------------------------------------------------

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        regex_out, regex_map = self._regex.anonymize(text)

        # Regex placeholders use letters E/F/U/M/D/A/C/S; NER uses P/O/G/M/D/K.
        # The shared letters M (money) and D (date) DO collide: a regex
        # ``Date_D1`` and an NER ``Date_D1`` would be the same key, and the
        # merge below would silently drop one mapping. Seed the NER pass with
        # the regex pass's per-letter counts so it continues numbering
        # (``Date_D2``, …) and every placeholder stays globally unique.
        ner_out, ner_map = self._ner.anonymize(
            regex_out, start_counters=_counts_from_mapping(regex_map)
        )

        merged = {**regex_map, **ner_map}
        return ner_out, merged

    def detect(self, text: str) -> list[Detection]:
        """Detections with offsets in the *original* text.

        Regex detections come back with original offsets directly. NER
        detections are produced on the regex-anonymized text, so each is
        re-anchored to the first matching uncovered occurrence of its value
        in the original.
        """
        regex_dets = self._regex.detect(text)
        regex_out, regex_map = self._regex.anonymize(text)
        ner_dets_anon = self._ner.detect(regex_out)

        # Defensive: drop any NER hit that landed on a regex placeholder.
        placeholders = set(regex_map.keys())
        ner_dets_anon = [d for d in ner_dets_anon if d.value not in placeholders]

        regex_spans = [(d.start, d.end) for d in regex_dets]
        mapped: list[Detection] = []
        for d in ner_dets_anon:
            claimed = regex_spans + [(m.start, m.end) for m in mapped]
            cursor = 0
            while True:
                idx = text.find(d.value, cursor)
                if idx < 0:
                    break
                s, e = idx, idx + len(d.value)
                if not any(s < ce and cs < e for cs, ce in claimed):
                    mapped.append(Detection(s, e, d.type, d.value))
                    break
                cursor = idx + 1

        return sorted(regex_dets + mapped, key=lambda d: d.start)
