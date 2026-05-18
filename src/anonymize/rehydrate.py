"""Inverse of the anonymizer pipeline: swap placeholders back to originals.

Anonymizers produce proper-noun-shaped placeholders (``Alex_P1``,
``Email_E1``, ``Westview_G2``, …). After Claude reasons over the
anonymized text, ``rehydrate()`` walks the output and substitutes each
placeholder back to its source value using the mapping returned by the
anonymizer.

Three wrinkles worth flagging:

- Possessives. ``Alex_P1's`` should become ``Sarah's``, not
  ``Sarah_P1's`` or ``Sarahs``. The trailing ``'s`` (straight or curly
  apostrophe) is captured separately and re-attached after lookup.
- Punctuation-adjacent placeholders. ``(Alex_P1)`` and ``Alex_P1,`` work
  for free — the placeholder shape is distinctive enough that
  surrounding punctuation never participates in the match.
- Unknown placeholders. Claude occasionally invents a new placeholder
  while paraphrasing (``Alex_P9`` when the mapping only has ``Alex_P1``
  and ``Alex_P2``). We pass those through verbatim and log a warning
  rather than raising — a draft that mentions a stray placeholder is
  recoverable; a crash mid-pipeline is not.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# Placeholder shape: Prefix_LetterN, e.g. Alex_P1, Email_E1, Westview_G2.
# A trailing possessive (straight ' or curly ’ apostrophe + s) is captured
# separately so we can strip it before lookup and re-attach after.
_PLACEHOLDER_RE = re.compile(r"([A-Z][a-z]+_[A-Z]\d+)(['’]s)?")


def rehydrate(text: str, mapping: dict[str, str]) -> str:
    """Replace placeholders in ``text`` with their original values.

    ``mapping`` is the dict returned by an anonymizer's ``anonymize()`` —
    placeholder → original value. Placeholders not present in the mapping
    are left in place and a warning is logged for each distinct unknown.
    """
    unknown: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        placeholder = match.group(1)
        possessive = match.group(2) or ""
        if placeholder not in mapping:
            unknown.add(placeholder)
            return match.group(0)
        return mapping[placeholder] + possessive

    out = _PLACEHOLDER_RE.sub(_sub, text)

    for ph in sorted(unknown):
        logger.warning("rehydrate: unknown placeholder %r left as-is", ph)

    return out
