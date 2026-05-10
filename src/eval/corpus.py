"""Loader for the hand-labeled sensitive-span corpus.

The corpus lives at ``data/eval/sensitive_spans.jsonl``. Each row:

    {
        "email_id": "eval-001",
        "text": "Hi Sarah, ...",
        "sensitive_spans": [
            {"start": 3, "end": 8, "type": "name", "value": "Sarah"},
            ...
        ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CORPUS_PATH = Path("data/eval/sensitive_spans.jsonl")


@dataclass
class SensitiveSpan:
    start: int
    end: int
    type: str
    value: str


@dataclass
class EvalExample:
    email_id: str
    text: str
    sensitive_spans: list[SensitiveSpan]


def load_corpus(path: str | Path | None = None) -> list[EvalExample]:
    p = Path(path) if path is not None else DEFAULT_CORPUS_PATH
    examples: list[EvalExample] = []
    with open(p, "r", encoding="utf-8") as f:
        for line_num, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{p}:{line_num}: invalid JSON ({e})") from e

            spans = [
                SensitiveSpan(
                    start=int(s["start"]),
                    end=int(s["end"]),
                    type=s["type"],
                    value=s["value"],
                )
                for s in row.get("sensitive_spans", [])
            ]
            example = EvalExample(
                email_id=row["email_id"],
                text=row["text"],
                sensitive_spans=spans,
            )
            _validate_spans(example, source=f"{p}:{line_num}")
            examples.append(example)
    return examples


def _validate_spans(ex: EvalExample, *, source: str) -> None:
    for span in ex.sensitive_spans:
        if not (0 <= span.start < span.end <= len(ex.text)):
            raise ValueError(
                f"{source}: span out of range for {ex.email_id}: "
                f"({span.start},{span.end}) text len={len(ex.text)}"
            )
        actual = ex.text[span.start:span.end]
        if actual != span.value:
            raise ValueError(
                f"{source}: span value mismatch for {ex.email_id}: "
                f"declared={span.value!r} actual={actual!r}"
            )
