"""Minimal .env loader — no python-dotenv dependency.

Reads ``KEY=VALUE`` lines from a ``.env`` file at the repo root and sets any
keys not already present in ``os.environ``. Intentionally tiny: no variable
interpolation, no ``export`` keyword, no multiline values, surrounding quotes
stripped. We only need it to surface ANTHROPIC_API_KEY (and friends) to the
Claude client and the evals.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root is the parent of this file's directory (src/).
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


def load_env_file(
    path: Path | str | None = None,
    *,
    override: bool = False,
) -> dict[str, str]:
    """Load KEY=VALUE pairs from ``path`` into ``os.environ``.

    Existing environment variables win unless ``override=True``. Returns the
    dict of keys found in the file (regardless of whether they were applied).
    Missing file is not an error — returns an empty dict.
    """
    p = Path(path) if path is not None else DEFAULT_ENV_PATH
    found: dict[str, str] = {}
    if not p.exists():
        return found

    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        found[key] = value
        # Treat an empty existing value as unset — some environments export
        # ANTHROPIC_API_KEY="" which would otherwise mask the .env value.
        if override or not os.environ.get(key):
            os.environ[key] = value

    return found
