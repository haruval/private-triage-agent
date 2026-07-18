"""Start the local review API and Vite UI as one supervised process.

The launcher intentionally keeps the two development servers separate while
giving users one entry point.  It waits for each fixed localhost port before
continuing, opens the browser only after Vite is ready, and stops both process
groups when the user exits.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend"
TOKEN_PATH = FRONTEND_DIR / ".dev-token"
API_ADDRESS = ("127.0.0.1", 8765)
WEB_ADDRESS = ("127.0.0.1", 5173)
WEB_URL = "http://localhost:5173"
STARTUP_TIMEOUT_SECONDS = 30.0
STOP_GRACE_SECONDS = 5.0


class StartupError(RuntimeError):
    """Raised when a prerequisite or child server fails during startup."""


def _venv_python() -> Path:
    """Return the platform-appropriate project virtualenv interpreter."""
    candidates = (
        REPO_ROOT / "venv" / "bin" / "python",
        REPO_ROOT / "venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise StartupError("venv is missing — run `make install` first")


def _check_prerequisites() -> tuple[Path, str]:
    """Validate setup and return the Python and npm executables to launch."""
    python = _venv_python()
    if not (FRONTEND_DIR / "node_modules").is_dir():
        raise StartupError(
            "frontend/node_modules is missing — run `cd frontend && npm install` first"
        )
    npm = shutil.which("npm")
    if npm is None:
        raise StartupError("npm is not on PATH — install Node.js 20 or newer")
    return python, npm


def _start(command: list[str], *, cwd: Path) -> subprocess.Popen[bytes]:
    """Start a child in its own group so its descendants can be stopped too."""
    if os.name == "nt":
        new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(command, cwd=cwd, creationflags=new_process_group)
    return subprocess.Popen(command, cwd=cwd, start_new_session=True)


def _port_is_open(address: tuple[str, int]) -> bool:
    """Return whether a localhost TCP listener accepts connections."""
    try:
        with socket.create_connection(address, timeout=0.2):
            return True
    except OSError:
        return False


def _ensure_ports_free() -> None:
    """Fail fast when either fixed port already has a listener.

    Readiness is later inferred from the same port probes, so a pre-existing
    listener (usually a stray server from a previous run) would otherwise be
    mistaken for the child this launcher just started.
    """
    for address, label in ((API_ADDRESS, "review API"), (WEB_ADDRESS, "web UI")):
        if _port_is_open(address):
            raise StartupError(
                f"port {address[1]} is already in use — another {label} instance "
                f"appears to be running (try `lsof -i :{address[1]}`); stop it and rerun"
            )


def _install_signal_handlers() -> None:
    """Turn termination signals into SystemExit so the cleanup block runs.

    Without this, SIGTERM (or the terminal closing, via SIGHUP) kills the
    launcher outright and the detached children survive holding their ports.
    """

    def _raise_exit(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _raise_exit)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _raise_exit)


def _wait_until_ready(
    process: subprocess.Popen[bytes],
    address: tuple[str, int],
    label: str,
    *,
    token_required: bool = False,
    previous_token: str | None = None,
) -> None:
    """Wait for a child listener, failing early if the child exits."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise StartupError(f"{label} exited during startup (status {return_code})")
        token_ready = True
        if token_required:
            try:
                token_ready = TOKEN_PATH.read_text(encoding="utf-8") != previous_token
            except OSError:
                token_ready = False
        if _port_is_open(address) and token_ready:
            return
        time.sleep(0.1)
    raise StartupError(
        f"timed out waiting for {label} on http://{address[0]}:{address[1]}"
    )


def _kill_group(pgid: int, sig: int) -> bool:
    """Signal a POSIX process group; return False when it no longer exists."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a child process group, escalating after a short grace period."""
    if process is None:
        return
    if os.name == "nt":
        # Windows has no group kill without a Job Object; stopping the
        # tracked process is the best this launcher does there.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return
    # Signal the whole group even when the tracked child itself has already
    # exited: descendants it spawned stay in the group and can outlive it.
    if not _kill_group(process.pid, signal.SIGTERM):
        process.wait()
        return
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None and not _kill_group(process.pid, 0):
            return
        time.sleep(0.1)
    _kill_group(process.pid, signal.SIGKILL)
    process.wait()


def _supervise(
    api_process: subprocess.Popen[bytes], web_process: subprocess.Popen[bytes]
) -> int:
    """Remain in the foreground until interrupted or either server exits."""
    while True:
        api_status = api_process.poll()
        if api_status is not None:
            print(f"API stopped (status {api_status}).", file=sys.stderr)
            return api_status or 1
        web_status = web_process.poll()
        if web_status is not None:
            print(f"Web server stopped (status {web_status}).", file=sys.stderr)
            return web_status or 1
        time.sleep(0.25)


def main(argv: list[str] | None = None) -> int:
    """Launch both local servers, open the UI, and supervise their lifetime."""
    parser = argparse.ArgumentParser(
        prog="python triage",
        description="Start the private triage web app and open it in a browser.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start both servers without opening the default browser.",
    )
    args = parser.parse_args(argv)
    _install_signal_handlers()

    api_process: subprocess.Popen[bytes] | None = None
    web_process: subprocess.Popen[bytes] | None = None
    try:
        python, npm = _check_prerequisites()
        _ensure_ports_free()
        print("Starting review API…")
        try:
            previous_token = TOKEN_PATH.read_text(encoding="utf-8")
        except OSError:
            previous_token = None
        api_process = _start([str(python), "-u", "-m", "src.api.server"], cwd=REPO_ROOT)
        _wait_until_ready(
            api_process,
            API_ADDRESS,
            "API",
            token_required=True,
            previous_token=previous_token,
        )

        print("Starting web UI…")
        web_process = _start([npm, "run", "dev"], cwd=FRONTEND_DIR)
        _wait_until_ready(web_process, WEB_ADDRESS, "web UI")

        print(f"Triage is ready at {WEB_URL}")
        print("Press Ctrl-C to stop both servers.")
        if not args.no_browser and not webbrowser.open(WEB_URL):
            print(f"Could not open the default browser; visit {WEB_URL}")
        return _supervise(api_process, web_process)
    except StartupError as exc:
        print(f"triage: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopping triage…")
        return 0
    finally:
        _stop_process(web_process)
        _stop_process(api_process)
