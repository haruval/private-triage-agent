"""Tests for the one-command local web launcher."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from src import launcher


def test_check_prerequisites_reports_missing_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)

    with pytest.raises(launcher.StartupError, match="make install"):
        launcher._check_prerequisites()


def test_wait_until_ready_requires_api_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = Mock()
    process.poll.return_value = None
    states = iter([False, True])
    token_path = tmp_path / ".dev-token"
    token_path.touch()
    monkeypatch.setattr(launcher, "TOKEN_PATH", token_path)
    monkeypatch.setattr(launcher, "_port_is_open", lambda _address: next(states))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    launcher._wait_until_ready(
        process,
        launcher.API_ADDRESS,
        "API",
        token_required=True,
        previous_token="old-token",
    )


def test_wait_until_ready_fails_when_child_exits() -> None:
    process = Mock()
    process.poll.return_value = 2

    with pytest.raises(launcher.StartupError, match="status 2"):
        launcher._wait_until_ready(process, launcher.WEB_ADDRESS, "web UI")


def test_main_starts_in_order_and_opens_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = Mock()
    web = Mock()
    starts = iter([api, web])
    events: list[str] = []

    monkeypatch.setattr(launcher, "_check_prerequisites", lambda: (Path("python"), "npm"))
    monkeypatch.setattr(
        launcher,
        "_start",
        lambda command, cwd: events.append(f"start:{command[-1]}") or next(starts),
    )
    monkeypatch.setattr(
        launcher,
        "_wait_until_ready",
        lambda process, address, label, **kwargs: events.append(f"ready:{label}"),
    )
    monkeypatch.setattr(
        launcher.webbrowser,
        "open",
        lambda url: events.append(f"browser:{url}") or True,
    )
    monkeypatch.setattr(launcher, "_supervise", lambda *_processes: 0)
    monkeypatch.setattr(launcher, "_stop_process", lambda _process: None)

    assert launcher.main([]) == 0
    assert events == [
        "start:src.api.server",
        "ready:API",
        "start:dev",
        "ready:web UI",
        f"browser:{launcher.WEB_URL}",
    ]


def test_main_no_browser_skips_open(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = iter([Mock(), Mock()])
    opened = Mock()
    monkeypatch.setattr(launcher, "_check_prerequisites", lambda: (Path("python"), "npm"))
    monkeypatch.setattr(launcher, "_start", lambda *_args, **_kwargs: next(processes))
    monkeypatch.setattr(launcher, "_wait_until_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_supervise", lambda *_args: 0)
    monkeypatch.setattr(launcher, "_stop_process", lambda _process: None)
    monkeypatch.setattr(launcher.webbrowser, "open", opened)

    assert launcher.main(["--no-browser"]) == 0
    opened.assert_not_called()
