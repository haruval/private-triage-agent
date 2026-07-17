#!/usr/bin/env python3
"""Age-check and cache the pinned coref model for verified offline use."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.anonymize.coref_anonymizer import cache_coref_model


def main() -> int:
    path = cache_coref_model()
    print(f"Coreference model cached at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
