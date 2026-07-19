#!/usr/bin/env python3
"""Verify every locked dependency and release artifact is old enough.

Supply-chain defense: malicious releases typically get reported and yanked
within a few weeks. By refusing to install locked requirements younger than
``MIN_AGE_DAYS`` (default 14), we skip the freshest poisoning window. PyPI
pins use upload metadata; hashed GitHub release wheels use release metadata.
Unknown metadata and unsupported lockfile shapes fail closed.

Usage::

    python scripts/check_package_ages.py [path/to/lockfile]

Environment::

    MIN_AGE_DAYS=14         # tune the threshold
    ALLOW_RECENT_PACKAGES=1 # bypass entirely (use deliberately)

Bypass::

    ALLOW_RECENT_PACKAGES=1 make install

Exits 0 on pass, 1 if metadata is unavailable or any release is too young,
and 2 on argument or lockfile errors.

Stdlib only — no third-party deps, so this can run on system Python before
the project's venv exists.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_LOCKFILE = "requirements.lock.txt"
DEFAULT_MIN_AGE_DAYS = 14
PYPI_TIMEOUT_SECS = 10
WORKERS = 10

_PYPI_REQ_LINE = re.compile(
    r"^([A-Za-z0-9_.\-]+)"     # package name
    r"(?:\[[^\]]+\])?"          # optional extras like [tests]
    r"==([^\s;]+)"              # ==version
    r"$"
)
_DIRECT_REQ_LINE = re.compile(
    r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?\s*@\s*(https://\S+)$"
)
_GITHUB_RELEASE_URL = re.compile(
    r"https://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"releases/download/(?P<tag>[^/?#]+)/(?P<filename>[^?#]+)"
    r"#sha256=(?P<digest>[0-9a-f]{64})"
)


@dataclass(frozen=True)
class LockedRequirement:
    """One validated lockfile entry and its authoritative metadata source."""

    name: str
    version: str
    source: str
    metadata_url: str
    artifact_name: str | None = None
    artifact_url: str | None = None
    sha256: str | None = None

    @property
    def display(self) -> str:
        separator = "==" if self.source == "pypi" else " @ "
        return f"{self.name}{separator}{self.version}"


def parse_lockfile(path: Path) -> list[LockedRequirement]:
    """Parse every dependency, rejecting entries the age gate cannot verify."""
    out: list[LockedRequirement] = []
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = re.sub(r"\s+#.*$", "", raw_line).strip()
        if not line or line.startswith("#"):
            continue
        pypi_match = _PYPI_REQ_LINE.fullmatch(line)
        if pypi_match:
            name, version = pypi_match.groups()
            out.append(
                LockedRequirement(
                    name=name,
                    version=version,
                    source="pypi",
                    metadata_url=f"https://pypi.org/pypi/{name}/{version}/json",
                )
            )
            continue

        direct_match = _DIRECT_REQ_LINE.fullmatch(line)
        if direct_match:
            name, url = direct_match.groups()
            github_match = _GITHUB_RELEASE_URL.fullmatch(url)
            if github_match is None:
                raise ValueError(
                    f"line {line_number}: direct requirement must be an immutable "
                    "GitHub release URL with a lowercase SHA-256 fragment"
                )
            owner = github_match.group("owner")
            repo = github_match.group("repo")
            tag = github_match.group("tag")
            filename = github_match.group("filename")
            digest = github_match.group("digest")
            out.append(
                LockedRequirement(
                    name=name,
                    version=tag,
                    source="github-release",
                    metadata_url=(
                        f"https://api.github.com/repos/{owner}/{repo}/releases/tags/"
                        f"{quote(tag, safe='')}"
                    ),
                    artifact_name=filename,
                    artifact_url=url.split("#", 1)[0],
                    sha256=digest,
                )
            )
            continue

        raise ValueError(
            f"line {line_number}: unsupported or unpinned requirement {line!r}"
        )
    return out


def fetch_upload_time(requirement: LockedRequirement) -> datetime | None:
    """Return the official publication time, or ``None`` on any uncertainty."""
    req = Request(
        requirement.metadata_url,
        headers={
            "Accept": "application/vnd.github+json, application/json",
            "User-Agent": "pkg-age-check/1.0",
        },
    )
    try:
        with urlopen(req, timeout=PYPI_TIMEOUT_SECS) as f:
            data = json.load(f)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    if requirement.source == "github-release":
        if not isinstance(data, dict):
            return None
        assets = data.get("assets")
        if not isinstance(assets, list):
            return None
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if asset.get("name") != requirement.artifact_name:
                continue
            if asset.get("browser_download_url") != requirement.artifact_url:
                return None
            remote_digest = asset.get("digest")
            if (
                remote_digest is not None
                and remote_digest != f"sha256:{requirement.sha256}"
            ):
                return None
            return _parse_timestamp(asset.get("created_at"))
        return None

    times: list[datetime] = []
    urls = data.get("urls", []) if isinstance(data, dict) else []
    for u in urls:
        if not isinstance(u, dict):
            continue
        raw = u.get("upload_time_iso_8601") or u.get("upload_time")
        parsed = _parse_timestamp(raw)
        if parsed is not None:
            times.append(parsed)
    # A new wheel can be attached to an old PyPI version. Pip may select that
    # wheel for this machine, so every published distribution must age past
    # the policy window rather than only the first upload.
    return max(times) if times else None


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


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
    if min_age_days < 0:
        print("MIN_AGE_DAYS must not be negative", file=sys.stderr)
        return 2
    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)

    try:
        pkgs = parse_lockfile(lockfile)
    except (OSError, ValueError) as exc:
        print(f"Could not validate {lockfile}: {exc}", file=sys.stderr)
        return 2
    if not pkgs:
        print(f"No pinned packages found in {lockfile}.")
        return 0

    print(
        f"Checking {len(pkgs)} locked requirements in {lockfile} "
        f"(min age: {min_age_days} days)..."
    )

    results: dict[LockedRequirement, datetime | None] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(fetch_upload_time, requirement): requirement
            for requirement in pkgs
        }
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    too_young: list[tuple[LockedRequirement, datetime]] = []
    unknown: list[LockedRequirement] = []
    for requirement, uploaded in results.items():
        if uploaded is None:
            unknown.append(requirement)
        elif uploaded > cutoff:
            too_young.append((requirement, uploaded))

    if unknown:
        print(
            f"\nFAIL: {len(unknown)} requirement(s) had no trustworthy release "
            "metadata:"
        )
        for requirement in sorted(unknown, key=lambda item: item.display.lower()):
            print(f"  {requirement.display}   metadata: {requirement.metadata_url}")
        print(
            "\nThe age gate fails closed. Retry when metadata is available or, "
            "after deliberate review, use:\n"
            "  ALLOW_RECENT_PACKAGES=1 make install"
        )
        return 1

    if too_young:
        print(
            f"\nFAIL: {len(too_young)} package(s) released within the last "
            f"{min_age_days} days:"
        )
        too_young.sort(key=lambda r: r[1], reverse=True)
        now = datetime.now(timezone.utc)
        for requirement, when in too_young:
            age = (now - when).days
            print(f"  {requirement.display}   published {when.date()} ({age}d ago)")
        print(
            "\nSupply-chain attacks often surface within ~2 weeks of upload.\n"
            "Wait it out, downgrade to an older pin, or override with:\n"
            "  ALLOW_RECENT_PACKAGES=1 make install"
        )
        return 1

    print(
        f"\nOK: all {len(pkgs)} locked requirements are at least "
        f"{min_age_days} days old."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
