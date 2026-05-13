"""Coreference-aware anonymizer.

Wraps :class:`CombinedAnonymizer` (regex + NER) with a third pass that uses
fastcoref to find pronoun chains pointing to already-tagged entities, then
replaces those pronouns with the entity's placeholder.

# Library choice: fastcoref over spaCy's experimental coref.
#
# spaCy's experimental coref (``spacy-experimental==0.6.4``, model
# ``en_coreference_web_trf``) ships only as a source distribution and fails
# to build on Python 3.12 / Apple Silicon — its Cython output references
# ``_PyCFrame->use_tracing``, which was removed from CPython 3.12.
# ``fastcoref`` installs cleanly on Apple Silicon as a pure-Python wheel
# (after pinning ``transformers<5`` for the ``FCorefModel.all_tied_weights_keys``
# attribute introduced in transformers 5.x).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.anonymize.ner_anonymizer import CombinedAnonymizer
from src.anonymize.regex_anonymizer import Detection

PRONOUNS: frozenset[str] = frozenset({
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "they", "them", "their", "theirs", "themselves",
    "it", "its", "itself",
})

DEFAULT_COREF_MODEL = "biu-nlp/f-coref"


@dataclass(frozen=True)
class _Replacement:
    start: int
    end: int
    placeholder: str
    value: str
    source: str  # "base" or "coref"


class CorefAnonymizer:
    """Regex + NER + coref pronoun resolution.

    Public surface mirrors :class:`CombinedAnonymizer`. ``detect()`` returns
    every span we replace — base detections plus pronoun mentions resolved
    via coref — all anchored to offsets in the *original* text.
    """

    def __init__(
        self,
        *,
        base: CombinedAnonymizer | None = None,
        coref: Any = None,
        coref_model_name: str = DEFAULT_COREF_MODEL,
    ) -> None:
        self._base = base if base is not None else CombinedAnonymizer()
        if coref is not None:
            self._coref = coref
        else:
            from fastcoref import FCoref  # lazy: avoids HF + torch import at module load
            self._coref = FCoref(model_name_or_path=coref_model_name)

    # --- public API -------------------------------------------------------

    def anonymize(self, text: str) -> tuple[str, dict[str, str]]:
        reps, base_map = self._build_replacements(text)
        out = text
        for r in sorted(reps, key=lambda r: r.start, reverse=True):
            out = out[: r.start] + r.placeholder + out[r.end :]
        return out, base_map

    def detect(self, text: str) -> list[Detection]:
        reps, _ = self._build_replacements(text)
        type_for = {"base": None, "coref": "pronoun"}
        return [
            Detection(r.start, r.end, type_for[r.source] or "base", r.value)
            for r in sorted(reps, key=lambda r: r.start)
        ]

    # --- internals --------------------------------------------------------

    def _build_replacements(
        self, text: str
    ) -> tuple[list[_Replacement], dict[str, str]]:
        base_dets = self._base.detect(text)
        _, base_map = self._base.anonymize(text)
        value_to_placeholder = {v: k for k, v in base_map.items()}

        base_reps: list[_Replacement] = []
        for d in base_dets:
            ph = value_to_placeholder.get(d.value)
            if ph is None:
                continue
            base_reps.append(_Replacement(d.start, d.end, ph, d.value, "base"))

        # Coref runs on the ORIGINAL text — pronouns and their antecedents
        # must both be visible.
        clusters = self._coref.predict(texts=[text])[0].get_clusters(as_strings=False)

        base_spans = [(r.start, r.end) for r in base_reps]
        coref_reps: list[_Replacement] = []
        for cluster in clusters:
            placeholder = _placeholder_for_cluster(cluster, base_reps)
            if placeholder is None:
                continue
            for (cs, ce) in cluster:
                mention = text[cs:ce]
                if mention.lower().strip() not in PRONOUNS:
                    continue
                if _overlaps_any(cs, ce, base_spans):
                    continue
                if _overlaps_any(cs, ce, [(r.start, r.end) for r in coref_reps]):
                    continue
                coref_reps.append(_Replacement(cs, ce, placeholder, mention, "coref"))

        return base_reps + coref_reps, base_map


def _placeholder_for_cluster(
    cluster: list[tuple[int, int]],
    base_reps: list[_Replacement],
) -> str | None:
    """Return the placeholder for the first cluster mention that overlaps
    a base detection. ``None`` if no mention overlaps any base span."""
    for (cs, ce) in cluster:
        for r in base_reps:
            if cs < r.end and r.start < ce:
                return r.placeholder
    return None


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for cs, ce in spans:
        if start < ce and cs < end:
            return True
    return False
