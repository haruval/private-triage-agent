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
    monkeypatch.setattr(launcher, "FRONTEND_DIR", tmp_path / "frontend")

    with pytest.raises(launcher.StartupError, match="make install"):
        launcher._check_prerequisites()


def test_check_prerequisites_reports_missing_node_modules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "FRONTEND_DIR", tmp_path / "frontend")
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()

    with pytest.raises(launcher.StartupError, match="npm install"):
        launcher._check_prerequisites()


def test_ensure_ports_free_rejects_existing_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher, "_port_is_open", lambda _address: True)

    with pytest.raises(launcher.StartupError, match="already in use"):
        launcher._ensure_ports_free()


def test_stop_process_signals_group_after_child_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if launcher.os.name == "nt":
        pytest.skip("POSIX process-group semantics")
    # The tracked child has exited, but its group may hold live descendants:
    # the group must still get SIGTERM, and the existence probe ends the wait.
    process = Mock()
    process.pid = 4242
    process.poll.return_value = 0
    sent: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(launcher.os, "killpg", fake_killpg)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    launcher._stop_process(process)

    assert sent[0] == (4242, launcher.signal.SIGTERM)
    assert (4242, launcher.signal.SIGKILL) not in sent


def test_install_signal_handlers_raises_system_exit() -> None:
    previous = launcher.signal.getsignal(launcher.signal.SIGTERM)
    try:
        launcher._install_signal_handlers()
        handler = launcher.signal.getsignal(launcher.signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(SystemExit) as excinfo:
            handler(int(launcher.signal.SIGTERM), None)
        assert excinfo.value.code == 128 + int(launcher.signal.SIGTERM)
    finally:
        launcher.signal.signal(launcher.signal.SIGTERM, previous)


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

    monkeypatch.setattr(
        launcher, "_check_prerequisites", lambda: (Path("python"), "npm")
    )
    monkeypatch.setattr(launcher, "_ensure_ports_free", lambda: None)
    monkeypatch.setattr(launcher, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(
        launcher,
        "_start",
        lambda command, cwd: (
            events.append(f"start:{command[-1]}:{cwd}") or next(starts)
        ),
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
        f"start:src.api.server:{launcher.REPO_ROOT}",
        "ready:API",
        f"start:dev:{launcher.FRONTEND_DIR}",
        "ready:web UI",
        f"browser:{launcher.WEB_URL}",
    ]


def test_main_no_browser_skips_open(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = iter([Mock(), Mock()])
    opened = Mock()
    monkeypatch.setattr(
        launcher, "_check_prerequisites", lambda: (Path("python"), "npm")
    )
    monkeypatch.setattr(launcher, "_ensure_ports_free", lambda: None)
    monkeypatch.setattr(launcher, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(launcher, "_start", lambda *_args, **_kwargs: next(processes))
    monkeypatch.setattr(launcher, "_wait_until_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_supervise", lambda *_args: 0)
    monkeypatch.setattr(launcher, "_stop_process", lambda _process: None)
    monkeypatch.setattr(launcher.webbrowser, "open", opened)

    assert launcher.main(["--no-browser"]) == 0
    opened.assert_not_called()
