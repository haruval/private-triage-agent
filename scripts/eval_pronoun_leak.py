#!/usr/bin/env python3
"""Pronoun leak rate over the sensitive-spans eval corpus.

For each gold-labeled pronoun span in ``data/eval/sensitive_spans.jsonl``,
check whether the coref-aware anonymizer replaced it. Print per-example
and aggregate results.

Run:
    venv/bin/python scripts/eval_pronoun_leak.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.anonymize.coref_anonymizer import CorefAnonymizer
from src.eval.corpus import load_corpus


def _span_covered(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for cs, ce in spans:
        if start < ce and cs < end:
            return True
    return False


def main() -> int:
    examples = load_corpus()
    anon = CorefAnonymizer()

    total_pronouns = 0
    total_anonymized = 0
    per_example: list[tuple[str, int, int, list[str]]] = []

    for ex in examples:
        gold_pronouns = [s for s in ex.sensitive_spans if s.type == "pronoun"]
        if not gold_pronouns:
            continue

        detections = anon.detect(ex.text)
        covered_spans = [(d.start, d.end) for d in detections]

        leaked: list[str] = []
        caught = 0
        for p in gold_pronouns:
            if _span_covered(p.start, p.end, covered_spans):
                caught += 1
            else:
                leaked.append(p.value)

        total_pronouns += len(gold_pronouns)
        total_anonymized += caught
        per_example.append((ex.email_id, caught, len(gold_pronouns), leaked))

    print("Pronoun leak rate evaluation")
    print("=" * 60)
    print("(definition: fraction of gold-labeled pronouns referring to")
    print(" anonymized entities that themselves got anonymized)")
    print()
    for email_id, caught, total, leaked in per_example:
        rate = caught / total if total else 0.0
        leaked_str = f"  leaked: {leaked}" if leaked else ""
        print(f"  {email_id}: {caught}/{total} caught ({rate:.0%}){leaked_str}")

    print()
    if total_pronouns == 0:
        print("No gold pronouns in corpus.")
        return 0

    rate = total_anonymized / total_pronouns
    print(f"Aggregate: {total_anonymized}/{total_pronouns} pronouns anonymized "
          f"({rate:.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
