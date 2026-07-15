"""Local HTTP API for the web review queue — stdlib ``http.server`` only.

This server fronts the most sensitive data in the project: raw email bodies,
the IMAP app password, and (via the ``.env`` file it edits) ANTHROPIC_API_KEY.
Binding to 127.0.0.1 is **not** a security boundary — any web page open in the
user's browser can reach a localhost port through DNS rebinding or CSRF, using
the browser as a confused deputy. So every ``/api`` request passes one shared
gate before any handler runs:

1. exact ``Host`` allowlist (a DNS-rebound request carries the attacker's
   hostname, so it dies here);
2. a per-run session token in ``X-Triage-Token``, generated at startup and
   written 0600 to ``frontend/.dev-token`` — the Vite dev proxy injects it
   server-side, so browser JS (and therefore any foreign origin) never sees
   it;
3. an ``Origin`` allowlist on mutating requests (defense in depth);
4. hygiene: JSON-only POSTs, a body-size cap, ``nosniff`` + ``no-store`` on
   every response, and no CORS headers of any kind.

The handlers reuse the queue ledgers (`src.review_queue`) and the shared
persistence path (`src.review_actions`), so a web approval produces exactly
the artifacts a terminal `review` approval does. The placeholder → original
``mapping`` never leaves the process: queue responses carry only its size.
Nothing here ever sends mail.
"""

from __future__ import annotations

import argparse
import hmac
import imaplib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from src import review_queue
from src.config import REPO_ROOT, load_env_file
from src.review_actions import (
    append_session_record,
    persist_approved,
    processed_from_record,
    session_record,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_FILENAME = ".dev-token"

# Only the Vite dev server may originate mutating requests.
ALLOWED_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})

MAX_BODY_BYTES = 1024 * 1024        # 1 MiB request-body cap
MAX_DRAFT_CHARS = 100_000

REVIEW_ACTIONS = frozenset({"approve", "edit", "reject"})

IMAP_PROVIDER_TIMEOUT_SECS = 15

_PICK_MBOX_SCRIPT = """\
try
  set selectedFile to choose file with prompt "Choose an .mbox file"
  return POSIX path of selectedFile
on error number -128
  return ""
end try
"""

_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_FOLDER_RE = re.compile(r"^[A-Za-z0-9\[\]/&_. -]+$")


# ---------------------------------------------------------------------------
# Request validation (repo pattern: dataclass + from_json_dict)
# ---------------------------------------------------------------------------


def _require_clean_str(
    d: dict[str, Any], key: str, *, max_len: int, default: str | None = None
) -> str:
    """A string field with no control characters (defeats .env line injection)."""
    value = d.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string")
    if len(value) > max_len:
        raise ValueError(f"'{key}' too long (max {max_len} chars)")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"'{key}' must not contain control characters")
    return value


@dataclass(frozen=True)
class ReviewRequest:
    """One decision from the web reviewer, same choices as `_prompt_action`."""

    email_id: str
    action: str  # approve | edit | reject
    draft: str

    @classmethod
    def from_json_dict(cls, d: Any) -> "ReviewRequest":
        if not isinstance(d, dict):
            raise ValueError(f"Expected a JSON object, got {type(d).__name__}")
        email_id = d.get("email_id")
        if not isinstance(email_id, str) or not email_id.strip():
            raise ValueError("'email_id' must be a non-empty string")
        action = d.get("action")
        if action not in REVIEW_ACTIONS:
            raise ValueError(f"'action' must be one of {sorted(REVIEW_ACTIONS)}")
        draft = d.get("draft", "")
        if draft is None:
            draft = ""
        if not isinstance(draft, str):
            raise ValueError("'draft' must be a string")
        if len(draft) > MAX_DRAFT_CHARS:
            raise ValueError(f"'draft' too long (max {MAX_DRAFT_CHARS} chars)")
        return cls(email_id=email_id, action=action, draft=draft)


@dataclass(frozen=True)
class ImapSettings:
    """IMAP connection values from the settings form.

    An empty ``password`` means "keep whatever is already saved" so the form
    can update host/user/folder without the user retyping the app password.
    Every value is checked for control characters before it can reach the
    ``.env`` writer — the loader parses line-by-line, so an embedded newline
    would otherwise inject arbitrary keys.
    """

    host: str
    user: str
    password: str
    folder: str

    @classmethod
    def from_json_dict(cls, d: Any, *, require_host_user: bool = True) -> "ImapSettings":
        if not isinstance(d, dict):
            raise ValueError(f"Expected a JSON object, got {type(d).__name__}")
        host = _require_clean_str(d, "host", max_len=253, default="").strip()
        user = _require_clean_str(d, "user", max_len=320, default="").strip()
        password = _require_clean_str(d, "password", max_len=256, default="")
        folder = _require_clean_str(d, "folder", max_len=200, default="").strip()
        if not folder:
            folder = "INBOX"
        if require_host_user:
            if not host:
                raise ValueError("'host' must not be empty")
            if not user:
                raise ValueError("'user' must not be empty")
        if host and not _HOSTNAME_RE.match(host):
            raise ValueError("'host' is not a valid hostname")
        if not _FOLDER_RE.match(folder):
            raise ValueError("'folder' contains unsupported characters")
        return cls(host=host, user=user, password=password, folder=folder)


# ---------------------------------------------------------------------------
# .env read-modify-write
# ---------------------------------------------------------------------------


def write_env_keys(env_path: Path, updates: dict[str, str]) -> None:
    """Update ``KEY=VALUE`` lines in ``env_path``, preserving everything else.

    Matching keys are rewritten in place (later duplicates of an updated key
    are dropped so a stale value can't win at load time), unrelated lines and
    comments survive untouched, and missing keys are appended. The file is
    replaced atomically and always ends up mode 0600 — it holds credentials
    and must never be world-readable. Values are re-checked for control
    characters here as a last line of defense against .env injection.
    """
    for key, value in updates.items():
        if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
            raise ValueError(f"refusing to write suspicious env key {key!r}")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"refusing to write control characters into {key}")

    lines = (
        env_path.read_text(encoding="utf-8").splitlines()
        if env_path.exists()
        else []
    )
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        key = ""
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
        if key in updates:
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
            # else: a duplicate of a key we already rewrote — drop it.
            continue
        out.append(raw)
    for key, value in updates.items():
        if key in remaining:
            out.append(f"{key}={value}")

    tmp_path = env_path.with_name(env_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        os.chmod(tmp_path, 0o600)  # in case tmp_path pre-existed with wider mode
        os.replace(tmp_path, env_path)
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Server + config
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    """Paths and binding for one server run. Defaults anchor at the repo root."""

    queue_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "queue")
    approved_dir: Path = field(
        default_factory=lambda: REPO_ROOT / "data" / "approved_drafts"
    )
    sessions_dir: Path = field(default_factory=lambda: REPO_ROOT / "logs" / "sessions")
    env_path: Path = field(default_factory=lambda: REPO_ROOT / ".env")
    token_path: Path = field(
        default_factory=lambda: REPO_ROOT / "frontend" / TOKEN_FILENAME
    )
    inbox_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "inbox")
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


def _write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.chmod(path, 0o600)  # O_CREAT mode is ignored when the file pre-exists


class TriageAPIServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the per-run token, lock, and paths."""

    daemon_threads = True

    def __init__(self, config: ServerConfig) -> None:
        super().__init__((config.host, config.port), TriageRequestHandler)
        self.config = config
        self.token = secrets.token_urlsafe(32)
        # One lock serializes every write (ledger, session log, .env, drafts):
        # a torn .env read-modify-write could drop ANTHROPIC_API_KEY.
        self.write_lock = threading.Lock()
        self.session_path = (
            config.sessions_dir
            / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_web.jsonl"
        )
        # Exact-match Host allowlist, from the port actually bound (defeats
        # DNS rebinding: a rebound request still carries the attacker's name).
        self.allowed_hosts = frozenset(
            {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        )
        _write_token_file(config.token_path, self.token)

    def remove_token_file(self) -> None:
        try:
            self.config.token_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove %s", self.config.token_path)


def create_server(config: ServerConfig | None = None) -> TriageAPIServer:
    return TriageAPIServer(config or ServerConfig())


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class TriageRequestHandler(BaseHTTPRequestHandler):
    server: TriageAPIServer  # narrowed from BaseHTTPRequestHandler's socketserver type

    protocol_version = "HTTP/1.1"

    # --- plumbing ----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: int, message: str) -> None:
        # Rejections happen before the body is read; close the connection so
        # an unread body can't desync the next keep-alive request.
        self.close_connection = True
        self._send_json(status, {"error": message})

    # --- the shared security gate (runs before any handler) -----------------

    def _gate(self, method: str) -> bool:
        """Host allowlist, session token, origin allowlist, request hygiene.

        Applies to every ``/api`` request, GET and POST alike. Returns True
        when the request may proceed; otherwise the rejection has been sent.
        """
        host = self.headers.get("Host", "")
        if host not in self.server.allowed_hosts:
            self._reject(403, "forbidden: unexpected Host header")
            return False

        token = self.headers.get("X-Triage-Token", "")
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            self._reject(403, "forbidden: missing or invalid session token")
            return False

        if method == "POST":
            origin = self.headers.get("Origin")
            if origin is not None and origin not in ALLOWED_ORIGINS:
                self._reject(403, "forbidden: cross-origin request")
                return False

            ctype = (self.headers.get("Content-Type") or "").partition(";")[0]
            if ctype.strip().lower() != "application/json":
                self._reject(415, "Content-Type must be application/json")
                return False

            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._reject(400, "bad Content-Length")
                return False
            if length > MAX_BODY_BYTES:
                self._reject(413, "request body too large")
                return False

        return True

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length) if length > 0 else b""

    # --- routing ------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._gate("GET"):
            return
        try:
            if self.path == "/api/queue":
                self._handle_queue()
            elif self.path == "/api/settings/imap":
                self._handle_settings_get()
            else:
                self._reject(404, "not found")
        except Exception:
            logger.exception("GET %s failed", self.path)
            self._reject(500, "internal error")

    def do_POST(self) -> None:
        if not self._gate("POST"):
            return
        try:
            body = self._read_body()
            if self.path == "/api/review":
                self._handle_review(body)
            elif self.path == "/api/import-mbox":
                self._handle_import_mbox()
            elif self.path == "/api/settings/imap":
                self._handle_settings_post(body)
            elif self.path == "/api/settings/imap/test":
                self._handle_settings_test(body)
            else:
                self._reject(404, "not found")
        except Exception:
            logger.exception("POST %s failed", self.path)
            self._reject(500, "internal error")

    def _method_not_allowed(self) -> None:
        if not self._gate("GET"):
            return
        self._reject(405, "method not allowed")

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    # --- GET /api/queue ------------------------------------------------------

    def _handle_queue(self) -> None:
        pending = review_queue.pending_records(self.server.config.queue_dir)
        self._send_json(200, [_record_dto(rec) for rec in pending])

    # --- POST /api/review ----------------------------------------------------

    def _handle_review(self, body: bytes) -> None:
        try:
            req = ReviewRequest.from_json_dict(json.loads(body or b"null"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        config = self.server.config
        with self.server.write_lock:
            pending = review_queue.pending_records(config.queue_dir)
            rec = next((r for r in pending if r.email.id == req.email_id), None)
            if rec is None:
                self._send_json(
                    400, {"error": "unknown or already-reviewed email_id"}
                )
                return

            p = processed_from_record(rec)
            saved_path: Path | None = None
            note: str | None = None
            warning: str | None = None
            if req.action in ("approve", "edit"):
                # Mirror `_prompt_action`: a record the pipeline produced no
                # draft for can only be rejected/skipped.
                if not (rec.draft and rec.draft.strip()):
                    self._send_json(
                        400, {"error": "record has no draft — only reject is available"}
                    )
                    return
                if not req.draft.strip():
                    self._send_json(
                        400, {"error": f"cannot {req.action} with an empty draft"}
                    )
                    return
                outcome = persist_approved(
                    p, req.draft, config.approved_dir, rec.source
                )
                saved_path = outcome.txt_path
                note = outcome.note
                warning = outcome.warning

            review_queue.append_reviewed(
                config.queue_dir, rec.email.id, req.action, saved_path
            )
            append_session_record(
                self.server.session_path, session_record(p, req.action, saved_path)
            )

        self._send_json(
            200,
            {
                "ok": True,
                "action": req.action,
                "saved_path": str(saved_path) if saved_path else None,
                "note": note,
                "warning": warning,
            },
        )

    # --- POST /api/import-mbox ------------------------------------------------

    def _handle_import_mbox(self) -> None:
        """Choose an mbox in a native dialog and copy it into ``data/inbox``.

        The selected path comes only from a fixed AppleScript; any
        client-supplied body is ignored. Existing inbox files are never
        overwritten — a numeric suffix is added when names collide.
        """
        try:
            result = subprocess.run(  # list form, fixed argv, no shell
                ["osascript", "-e", _PICK_MBOX_SCRIPT],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._send_json(500, {"error": "could not open the .mbox file picker"})
            return
        if result.returncode != 0:
            self._send_json(500, {"error": "could not open the .mbox file picker"})
            return

        selected = result.stdout.rstrip("\r\n")
        if not selected:
            self._send_json(200, {"ok": True, "cancelled": True, "path": None})
            return
        if any(ord(c) < 32 or ord(c) == 127 for c in selected):
            self._send_json(400, {"error": "selected path contains control characters"})
            return

        source = Path(selected)
        if not source.is_absolute():
            self._send_json(400, {"error": "selected path must be absolute"})
            return
        try:
            source = source.resolve(strict=True)
        except OSError:
            self._send_json(400, {"error": "selected file does not exist"})
            return
        if not source.is_file():
            self._send_json(400, {"error": "selected path is not a file"})
            return
        if source.suffix.lower() != ".mbox":
            self._send_json(400, {"error": "selected file must end in .mbox"})
            return

        inbox = self.server.config.inbox_dir
        with self.server.write_lock:
            inbox.mkdir(parents=True, exist_ok=True)
            destination = inbox / f"{source.stem}.mbox"
            if source == destination.resolve(strict=False):
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "cancelled": False,
                        "path": str(destination),
                        "filename": destination.name,
                    },
                )
                return
            suffix = 2
            while destination.exists():
                destination = inbox / f"{source.stem}-{suffix}.mbox"
                suffix += 1
            try:
                shutil.copy2(source, destination)
            except OSError:
                self._send_json(500, {"error": "could not copy the selected .mbox"})
                return

        self._send_json(
            200,
            {
                "ok": True,
                "cancelled": False,
                "path": str(destination),
                "filename": destination.name,
            },
        )

    # --- GET/POST /api/settings/imap -------------------------------------------

    def _handle_settings_get(self) -> None:
        self._send_json(
            200,
            {
                "host": os.environ.get("IMAP_HOST", "").strip(),
                "user": os.environ.get("IMAP_USER", "").strip(),
                "folder": os.environ.get("IMAP_FOLDER", "").strip() or "INBOX",
                # The password itself is write-only: report presence, never value.
                "password": "set" if os.environ.get("IMAP_PASS") else "unset",
            },
        )

    def _handle_settings_post(self, body: bytes) -> None:
        try:
            settings = ImapSettings.from_json_dict(json.loads(body or b"null"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        updates = {
            "IMAP_HOST": settings.host,
            "IMAP_USER": settings.user,
            "IMAP_FOLDER": settings.folder,
        }
        if settings.password:
            updates["IMAP_PASS"] = settings.password
        with self.server.write_lock:
            write_env_keys(self.server.config.env_path, updates)
        os.environ.update(updates)

        self._send_json(
            200,
            {
                "ok": True,
                "password": "set" if os.environ.get("IMAP_PASS") else "unset",
            },
        )

    # --- POST /api/settings/imap/test --------------------------------------------

    def _handle_settings_test(self, body: bytes) -> None:
        """Read-only connection check: TLS connect, login, select the folder.

        Values missing from the request fall back to the saved environment,
        so "Test connection" works both before and after saving. Uses
        ``IMAP4_SSL`` with default (verified) certificates. Never marks read,
        never deletes, never sends — and error strings never echo the
        username or password.
        """
        try:
            settings = ImapSettings.from_json_dict(
                json.loads(body or b"{}") or {}, require_host_user=False
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        host = settings.host or os.environ.get("IMAP_HOST", "").strip()
        user = settings.user or os.environ.get("IMAP_USER", "").strip()
        password = settings.password or os.environ.get("IMAP_PASS", "")

        missing = [
            name
            for name, value in (
                ("host", host),
                ("username", user),
                ("app password", password),
            )
            if not value
        ]
        if missing:
            self._send_json(
                200, {"ok": False, "error": f"missing: {', '.join(missing)}"}
            )
            return

        ok, message = _imap_check(host, user, password, settings.folder)
        if ok:
            self._send_json(200, {"ok": True, "message": message})
        else:
            self._send_json(200, {"ok": False, "error": message})


def _imap_check(host: str, user: str, password: str, folder: str) -> tuple[bool, str]:
    """Connect / login / select read-only; report a credential-free message."""
    try:
        client = imaplib.IMAP4_SSL(host, timeout=IMAP_PROVIDER_TIMEOUT_SECS)
    except OSError as exc:
        return False, f"could not connect to {host}: {exc}"

    try:
        try:
            client.login(user, password)
        except imaplib.IMAP4.error:
            # Deliberately fixed text: server error strings can be verbose and
            # the credentials must never round-trip into a response or log.
            return False, (
                "login failed — check the username and app password "
                "(use an app-specific password, not the account password)"
            )
        status, data = client.select(folder, readonly=True)
        if status != "OK":
            return False, f"logged in, but could not open folder {folder!r}"
        try:
            count = int(data[0] or b"0")
        except (TypeError, ValueError):
            count = 0
        return True, f"connected — folder {folder!r} has {count} message(s)"
    except OSError as exc:
        return False, f"connection to {host} failed: {type(exc).__name__}"
    finally:
        try:
            client.logout()
        except Exception:
            logger.debug("IMAP logout failed during connection test", exc_info=True)


# ---------------------------------------------------------------------------
# Queue response DTO
# ---------------------------------------------------------------------------


def _record_dto(rec: review_queue.QueueRecord) -> dict[str, Any]:
    """Serialize one pending record for the UI — **never** via ``to_json_dict``.

    ``QueueRecord.to_json_dict`` carries ``mapping`` (placeholder → original
    PII, the exact secret this product exists to protect) and raw ``headers``.
    The UI needs neither: the stored draft is already rehydrated server-side,
    so the panel only shows how many placeholders were involved.
    """
    return {
        "email": {
            "id": rec.email.id,
            "from_addr": rec.email.from_addr,
            "to_addrs": rec.email.to_addrs,
            "subject": rec.email.subject,
            "date": rec.email.date.isoformat(),
            "body_plain": rec.email.body_plain,
            "thread_id": rec.email.thread_id,
        },
        "result": {
            "category": rec.result.category,
            "confidence": rec.result.confidence,
            "summary": rec.result.summary,
            "action_items": rec.result.extracted_action_items,
            "reasoning": rec.result.reasoning,
        },
        "decision": {
            "escalate": rec.decision.escalate,
            "reason": rec.decision.reason,
            "score": rec.decision.score,
        },
        "draft": rec.draft,
        "provenance": rec.provenance,
        "claude_used": rec.claude_used,
        "error": rec.error,
        "importance": rec.importance,
        "importance_reason": rec.importance_reason,
        "ranked_by": rec.ranked_by,
        "source": rec.source,
        "placeholder_count": len(rec.mapping),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.api.server",
        description=(
            "Local, single-user web API for the review queue. Binds "
            f"{DEFAULT_HOST}:{DEFAULT_PORT}; access requires the per-run token "
            f"written to frontend/{TOKEN_FILENAME}. Start this before the Vite "
            "dev server (`make api`, then `make web`)."
        ),
    )
    parser.add_argument(
        "--queue-dir",
        type=str,
        default=None,
        help="Directory for the processed/reviewed ledgers (default: data/queue)",
    )
    parser.add_argument(
        "--approved-dir",
        type=str,
        default=None,
        help="Directory for approved drafts (default: data/approved_drafts)",
    )
    parser.add_argument(
        "--sessions-dir",
        type=str,
        default=None,
        help="Directory for per-run decision logs (default: logs/sessions)",
    )
    parser.add_argument(
        "--env-path",
        type=str,
        default=None,
        help="Path of the .env file the settings page edits (default: repo .env)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    config = ServerConfig()
    if args.queue_dir:
        config.queue_dir = Path(args.queue_dir)
    if args.approved_dir:
        config.approved_dir = Path(args.approved_dir)
    if args.sessions_dir:
        config.sessions_dir = Path(args.sessions_dir)
    if args.env_path:
        config.env_path = Path(args.env_path)
    load_env_file(config.env_path)

    try:
        server = create_server(config)
    except OSError as exc:
        print(f"could not bind {config.host}:{config.port}: {exc}", file=sys.stderr)
        return 1

    print(f"review API listening on http://{config.host}:{server.server_port}")
    print(f"  queue dir   : {config.queue_dir}")
    print(f"  session log : {server.session_path}")
    print(f"  token file  : {config.token_path} (mode 0600 — proxy-only)")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.remove_token_file()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
