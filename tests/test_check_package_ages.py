"""Tests for fail-closed dependency and external-artifact age validation."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import check_package_ages


SPACY_MODEL_LINE = (
    "en_core_web_trf @ https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_trf-3.8.0/en_core_web_trf-3.8.0-py3-none-any.whl#sha256="
    "272a31e9d8530d1e075351d30a462d7e80e31da23574f1b274e200f3fff35bf5"
)


def test_parse_lockfile_covers_pypi_and_hashed_github_release(tmp_path: Path) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text(f"requests==2.33.1\n{SPACY_MODEL_LINE}\n")

    requirements = check_package_ages.parse_lockfile(lockfile)

    assert [(item.name, item.source) for item in requirements] == [
        ("requests", "pypi"),
        ("en_core_web_trf", "github-release"),
    ]
    assert requirements[1].metadata_url.endswith(
        "/releases/tags/en_core_web_trf-3.8.0"
    )


@pytest.mark.parametrize(
    "line",
    [
        "demo @ https://example.com/demo.whl#sha256=" + "0" * 64,
        "demo @ https://github.com/example/demo/releases/download/v1/demo.whl",
        "demo>=1.0",
    ],
)
def test_parse_lockfile_rejects_unverifiable_requirements(
    tmp_path: Path, line: str
) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text(line + "\n")

    with pytest.raises(ValueError):
        check_package_ages.parse_lockfile(lockfile)


def test_github_release_uses_exact_asset_creation_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text(SPACY_MODEL_LINE + "\n")
    requirement = check_package_ages.parse_lockfile(lockfile)[0]
    payload = {
        "published_at": "2020-01-01T00:00:00Z",
        "assets": [{
            "name": requirement.artifact_name,
            "browser_download_url": requirement.artifact_url,
            "digest": f"sha256:{requirement.sha256}",
            "created_at": "2024-09-23T20:12:00Z",
        }],
    }
    monkeypatch.setattr(
        check_package_ages,
        "urlopen",
        lambda request, timeout: io.StringIO(json.dumps(payload)),
    )

    assert check_package_ages.fetch_upload_time(requirement) == datetime(
        2024, 9, 23, 20, 12, tzinfo=timezone.utc
    )


def test_github_release_rejects_malformed_or_wrong_asset_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text(SPACY_MODEL_LINE + "\n")
    requirement = check_package_ages.parse_lockfile(lockfile)[0]
    payload = {
        "assets": [{
            "name": requirement.artifact_name,
            "browser_download_url": "https://example.invalid/replaced.whl",
            "created_at": "not-a-timestamp",
        }],
    }
    monkeypatch.setattr(
        check_package_ages,
        "urlopen",
        lambda request, timeout: io.StringIO(json.dumps(payload)),
    )

    assert check_package_ages.fetch_upload_time(requirement) is None


def test_pypi_age_uses_newest_distribution_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text("demo==1.0\n")
    requirement = check_package_ages.parse_lockfile(lockfile)[0]
    payload = {
        "urls": [
            {"upload_time_iso_8601": "2020-01-01T00:00:00Z"},
            {"upload_time_iso_8601": "2026-07-15T00:00:00Z"},
        ]
    }
    monkeypatch.setattr(
        check_package_ages,
        "urlopen",
        lambda request, timeout: io.StringIO(json.dumps(payload)),
    )

    assert check_package_ages.fetch_upload_time(requirement) == datetime(
        2026, 7, 15, tzinfo=timezone.utc
    )


def test_main_fails_closed_when_metadata_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text("requests==2.33.1\n")
    monkeypatch.delenv("ALLOW_RECENT_PACKAGES", raising=False)
    monkeypatch.setattr(check_package_ages, "fetch_upload_time", lambda requirement: None)

    assert check_package_ages.main(["check_package_ages.py", str(lockfile)]) == 1


def test_main_accepts_old_pypi_and_github_release_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text(f"requests==2.33.1\n{SPACY_MODEL_LINE}\n")
    monkeypatch.delenv("ALLOW_RECENT_PACKAGES", raising=False)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    monkeypatch.setattr(check_package_ages, "fetch_upload_time", lambda requirement: old)

    assert check_package_ages.main(["check_package_ages.py", str(lockfile)]) == 0


def test_main_rejects_recent_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile = tmp_path / "requirements.lock.txt"
    lockfile.write_text("requests==2.33.1\n")
    monkeypatch.delenv("ALLOW_RECENT_PACKAGES", raising=False)
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(
        check_package_ages, "fetch_upload_time", lambda requirement: recent
    )

    assert check_package_ages.main(["check_package_ages.py", str(lockfile)]) == 1


def test_checked_in_lockfile_has_no_skipped_entries() -> None:
    requirements = check_package_ages.parse_lockfile(Path("requirements.lock.txt"))

    assert len(requirements) == 95
    assert any(item.name == "en_core_web_trf" for item in requirements)
