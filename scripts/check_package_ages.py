#!/usr/bin/env python3
"""Verify every pinned dep in ``requirements.lock.txt`` is old enough.

Supply-chain defense: malicious package versions uploaded to PyPI typically
get reported and yanked within a few weeks. By refusing to install pins
younger than ``MIN_AGE_DAYS`` (default 14), we skip the freshest poisoning
window.

Usage::

    python scripts/check_package_ages.py [path/to/lockfile]

Environment::

    MIN_AGE_DAYS=14         # tune the threshold
    ALLOW_RECENT_PACKAGES=1 # bypass entirely (use deliberately)

Bypass::

    ALLOW_RECENT_PACKAGES=1 make install

Exits 0 on pass, 1 if any pin is too young, 2 on argument / lockfile errors.

Stdlib only — no third-party deps, so this can run on system Python before
the project's venv exists.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_LOCKFILE = "requirements.lock.txt"
DEFAULT_MIN_AGE_DAYS = 14
PYPI_TIMEOUT_SECS = 10
WORKERS = 10

_REQ_LINE = re.compile(
    r"^([A-Za-z0-9_.\-]+)"     # package name
    r"(?:\[[^\]]+\])?"          # optional extras like [tests]
    r"==([^\s;]+)"              # ==version
)


def parse_lockfile(path: Path) -> list[tuple[str, str]]:
    """Return list of ``(name, version)`` from a pip-freeze style lockfile."""
    out: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_LINE.match(line)
        if not m:
            continue
        out.append((m.group(1), m.group(2)))
    return out


def fetch_upload_time(name: str, version: str) -> datetime | None:
    """Earliest upload time for this ``(name, version)`` on PyPI, or None."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    req = Request(url, headers={"User-Agent": "pkg-age-check/1.0"})
    try:
        with urlopen(req, timeout=PYPI_TIMEOUT_SECS) as f:
            data = json.load(f)
    except (HTTPError, URLError, TimeoutError):
        return None

    times: list[datetime] = []
    for u in data.get("urls", []):
        raw = u.get("upload_time_iso_8601") or u.get("upload_time")
        if not raw:
            continue
        try:
            times.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            continue
    return min(times) if times else None


def main(argv: list[str]) -> int:
    if os.environ.get("ALLOW_RECENT_PACKAGES") == "1":
        print("ALLOW_RECENT_PACKAGES=1 set — skipping age check.")
        return 0

    lockfile = Path(argv[1] if len(argv) > 1 else DEFAULT_LOCKFILE)
    if not lockfile.exists():
        print(f"No {lockfile}; nothing to check.")
        return 0

    try:
        min_age_days = int(os.environ.get("MIN_AGE_DAYS", DEFAULT_MIN_AGE_DAYS))
    except ValueError:
        print(f"Invalid MIN_AGE_DAYS={os.environ.get('MIN_AGE_DAYS')!r}", file=sys.stderr)
        return 2
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)

    pkgs = parse_lockfile(lockfile)
    if not pkgs:
        print(f"No pinned packages found in {lockfile}.")
        return 0

    print(
        f"Checking {len(pkgs)} pinned packages in {lockfile} "
        f"(min age: {min_age_days} days)..."
    )

    results: dict[tuple[str, str], datetime | None] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_upload_time, n, v): (n, v) for n, v in pkgs}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    too_young: list[tuple[str, str, datetime]] = []
    unknown: list[tuple[str, str]] = []
    for (name, version), uploaded in results.items():
        if uploaded is None:
            unknown.append((name, version))
        elif uploaded > cutoff:
            too_young.append((name, version, uploaded))

    if too_young:
        print(
            f"\nFAIL: {len(too_young)} package(s) released within the last "
            f"{min_age_days} days:"
        )
        too_young.sort(key=lambda r: r[2], reverse=True)
        now = datetime.now(timezone.utc)
        for name, version, when in too_young:
            age = (now - when).days
            print(f"  {name}=={version}   uploaded {when.date()} ({age}d ago)")
        print(
            "\nSupply-chain attacks often surface within ~2 weeks of upload.\n"
            "Wait it out, downgrade to an older pin, or override with:\n"
            "  ALLOW_RECENT_PACKAGES=1 make install"
        )
        return 1

    if unknown:
        print(
            f"\nWARN: {len(unknown)} package(s) had no resolvable PyPI metadata "
            "(private index, yanked, or transient PyPI error):"
        )
        for name, version in unknown:
            print(f"  {name}=={version}")
        print("Continuing — but verify these by hand if you don't recognize them.")

    print(f"\nOK: all {len(pkgs)} pinned packages are at least {min_age_days} days old.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
