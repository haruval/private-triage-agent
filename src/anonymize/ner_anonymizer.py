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

from dataclasses import dataclass
from typing import Any

from src.anonymize.regex_anonymizer import _PATTERNS, Detection, RegexAnonymizer

# spaCy entity label -> (type_name, placeholder_prefix, placeholder_letter)
_NER_LABELS: dict[str, tuple[str, str, str]] = {
    "PERSON": ("person",   "Alex",     "P"),
    "ORG":    ("org",      "Acme",     "O"),
    "GPE":    ("gpe",      "Westview", "G"),
    "MONEY":  ("money",    "Amount",   "M"),
    "DATE":   ("date",     "Date",     "D"),
    "FAC":    ("facility", "Beacon",   "K"),
}

# type_name -> (placeholder_prefix, placeholder_letter), per detection layer.
_NER_TYPE_META = {t: (prefix, letter) for t, prefix, letter in _NER_LABELS.values()}
_REGEX_TYPE_META = {t: (prefix, letter) for t, prefix, letter, _ in _PATTERNS}

DEFAULT_MODEL = "en_core_web_trf"


@dataclass(frozen=True)
class Replacement:
    """One planned substitution, anchored to offsets in the original text."""

    start: int
    end: int
    type: str
    placeholder: str
    value: str


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


def _apply_placeholders(
    text: str,
    hits: list[_RawHit],
) -> tuple[str, dict[str, str]]:
    """Assign sequential placeholders per letter and substitute right-to-left."""
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

    out = text
    for h in sorted(hits, key=lambda r: r.start, reverse=True):
        placeholder = value_to_placeholder[h.value]
        out = out[: h.start] + placeholder + out[h.end :]
    return out, mapping


def _map_to_original(
    pos: int,
    segments: list[tuple[int, int, int, int]],
    *,
    is_end: bool,
) -> int:
    """Translate a regex-cleaned-text offset back to the original text.

    ``segments`` are the regex substitutions as
    ``(clean_start, clean_end, orig_start, orig_end)`` in document order.
    An offset that falls inside a placeholder snaps outward — to the
    original span's start for span starts, its end for span ends — so a hit
    covering part of a placeholder always covers the whole original value.
    """
    shift = 0  # cleaned position minus original position, before each segment
    for c_start, c_end, o_start, o_end in segments:
        before = pos <= c_start if is_end else pos < c_start
        if before:
            return pos - shift
        inside = pos <= c_end if is_end else pos < c_end
        if inside:
            return o_end if is_end else o_start
        shift = c_end - o_end
    return pos - shift


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

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        hits = self._scan(text)
        return _apply_placeholders(text, hits)

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

    def replacements(self, text: str) -> tuple[list[Replacement], dict[str, str]]:
        """The authoritative substitution plan, anchored to the original text.

        Regex hits keep their true spans. NER hits are found on the
        regex-cleaned text and translated back by *offset* through the regex
        substitutions — never re-searched by value, which anchored repeated
        values to the wrong occurrence. An NER span that swallows a regex
        placeholder widens to the placeholder's full original value, and the
        longer span wins the overlap, so nested values stay covered.

        ``anonymize()``, ``detect()``, and the coref layer all consume this
        one plan; the returned mapping holds exactly the placeholders the
        plan applies, each keyed to its original-text value.
        """
        regex_dets = self._regex.detect(text)
        regex_out, regex_map = self._regex.anonymize(text)
        placeholder_for_value = {v: k for k, v in regex_map.items()}

        # Regex substitutions as (clean_start, clean_end, orig_start, orig_end).
        segments: list[tuple[int, int, int, int]] = []
        shift = 0
        for d in regex_dets:
            ph_len = len(placeholder_for_value[d.value])
            c_start = d.start + shift
            segments.append((c_start, c_start + ph_len, d.start, d.end))
            shift += ph_len - (d.end - d.start)

        # Regex hits first: on equal spans the stable sort in
        # _resolve_overlaps keeps them over the NER re-tag of a placeholder.
        hits: list[_RawHit] = []
        for d in regex_dets:
            prefix, letter = _REGEX_TYPE_META[d.type]
            hits.append(_RawHit(d.start, d.end, d.type, prefix, letter, d.value))
        for d in self._ner.detect(regex_out):
            s = _map_to_original(d.start, segments, is_end=False)
            e = _map_to_original(d.end, segments, is_end=True)
            value = text[s:e]
            if not value.strip():
                continue
            prefix, letter = _NER_TYPE_META[d.type]
            hits.append(_RawHit(s, e, d.type, prefix, letter, value))

        mapping: dict[str, str] = {}
        value_to_placeholder: dict[str, str] = {}
        counters: dict[str, int] = {}
        plan: list[Replacement] = []
        for h in _resolve_overlaps(hits):
            placeholder = value_to_placeholder.get(h.value)
            if placeholder is None:
                counters[h.letter] = counters.get(h.letter, 0) + 1
                placeholder = f"{h.prefix}_{h.letter}{counters[h.letter]}"
                value_to_placeholder[h.value] = placeholder
                mapping[placeholder] = h.value
            plan.append(Replacement(h.start, h.end, h.type, placeholder, h.value))
        return plan, mapping

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        plan, mapping = self.replacements(text)
        out = text
        for r in sorted(plan, key=lambda r: r.start, reverse=True):
            out = out[: r.start] + r.placeholder + out[r.end :]
        return out, mapping

    def detect(self, text: str) -> list[Detection]:
        """Non-overlapping detections with offsets in the *original* text."""
        plan, _ = self.replacements(text)
        return [Detection(r.start, r.end, r.type, r.value) for r in plan]
