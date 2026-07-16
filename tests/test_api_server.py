"""Tests for the web review API in src/api/server.py.

A real TriageAPIServer runs on an ephemeral loopback port for each test, with
every path (queue, approved drafts, sessions, .env, token) pointed at
tmp_path — requests go over actual HTTP so the security gate sees genuine
headers. The security tests are the point here: the server fronts raw email
bodies and credentials, and localhost is not a boundary. External effects
(IMAP, the native file picker) are monkeypatched; no test touches the network.
"""

from __future__ import annotations

import http.client
import imaplib
import json
import os
import socket
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from src import review_queue
from src.api.server import (
    ServerConfig,
    create_server,
    default_drafts_folder,
    write_env_keys,
)
from src.ingestion.mbox_loader import Email
from src.review_actions import save_approved_draft
from src.review_queue import ImapAccountRef, QueueRecord, compute_record_id
from src.router.sensitivity_scorer import EscalationDecision
from src.triage.classifier import TriageResult

SECRET_PHONE = "555-0182-SECRET"  # mapping value that must never reach a response

# Review requests are keyed by the opaque record_id; these mirror the fixture
# records below (mbox scope for <a>/<b>, unscoped-imap scope for legacy <c>).
RID_A = compute_record_id("mbox:test.mbox", None, "<a@host>")
RID_B = compute_record_id("mbox:test.mbox", None, "<b@host>")
RID_C = compute_record_id("imap", None, "<c@host>")


# ---------------------------------------------------------------------------
# Queue fixtures
# ---------------------------------------------------------------------------


def _email(email_id: str, subject: str = "Hi") -> Email:
    return Email(
        id=email_id,
        from_addr="alice@example.com",
        to_addrs=["me@example.com"],
        subject=subject,
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        body_plain="please call me",
        thread_id=None,
        headers={"References": "<x@y>"},
    )


def _result() -> TriageResult:
    return TriageResult(
        category="needs_reply",
        confidence=0.75,
        summary="Alice wants a call back.",
        extracted_action_items=["call alice"],
        suggested_reply_draft="local draft",
        reasoning="obviously a reply",
    )


def _record(
    email_id: str,
    *,
    draft: str | None,
    source: str = "mbox:test.mbox",
    importance: float = 5.0,
    mapping: dict[str, str] | None = None,
    imap_account: ImapAccountRef | None = None,
) -> QueueRecord:
    return QueueRecord(
        email=_email(email_id),
        result=_result(),
        decision=EscalationDecision(escalate=True, reason="keywords", score=0.9),
        draft=draft,
        provenance="Claude" if mapping else "local",
        mapping=mapping or {},
        claude_used=bool(mapping),
        error=None,
        importance=importance,
        importance_reason="deadline in it",
        ranked_by="Claude",
        source=source,
        processed_at="2026-07-01T10:00:00+00:00",
        imap_account=imap_account,
    )


@pytest.fixture()
def api(tmp_path: Path) -> Iterator["Api"]:
    queue_dir = tmp_path / "queue"
    review_queue.append_records(
        queue_dir,
        [
            _record(
                "<a@host>",
                draft="Thanks Sarah — will do.",
                importance=9.0,
                mapping={"Phone_F1": SECRET_PHONE},
            ),
            _record("<b@host>", draft=None, importance=5.0),
            _record("<c@host>", draft="ok, sounds good", source="imap", importance=3.0),
        ],
    )
    config = ServerConfig(
        queue_dir=queue_dir,
        approved_dir=tmp_path / "approved",
        sessions_dir=tmp_path / "sessions",
        env_path=tmp_path / ".env",
        token_path=tmp_path / "frontend" / ".dev-token",
        inbox_dir=tmp_path / "inbox",
        port=0,  # ephemeral — the handler allowlists the actually-bound port
    )
    server = create_server(config)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
    )
    thread.start()
    try:
        yield Api(server.server_port, server.token, config)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        server.remove_token_file()


_UNSET = object()


class Api:
    """Tiny raw-HTTP client so tests control every header the gate inspects."""

    def __init__(self, port: int, token: str, config: ServerConfig) -> None:
        self.port = port
        self.token = token
        self.config = config

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = _UNSET,
        raw_body: bytes | None = None,
        token: Any = _UNSET,
        host: str | None = None,
        origin: Any = _UNSET,
        content_type: str | None = "application/json",
    ) -> tuple[int, Any, dict[str, str]]:
        headers: dict[str, str] = {}
        tok = self.token if token is _UNSET else token
        if tok is not None:
            headers["X-Triage-Token"] = tok
        if host is not None:
            headers["Host"] = host
        request_origin = (
            "http://localhost:5173" if origin is _UNSET and method == "POST" else origin
        )
        if request_origin is not None and request_origin is not _UNSET:
            headers["Origin"] = request_origin

        data: bytes | None = None
        if raw_body is not None:
            data = raw_body
        elif body is not _UNSET:
            data = json.dumps(body).encode()
        if data is not None and content_type is not None:
            headers["Content-Type"] = content_type

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            parsed = json.loads(payload) if payload else None
            return resp.status, parsed, resp_headers
        finally:
            conn.close()

    def get(self, path: str, **kw: Any) -> tuple[int, Any, dict[str, str]]:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any, **kw: Any) -> tuple[int, Any, dict[str, str]]:
        return self.request("POST", path, body=body, **kw)


def _reviewed_lines(config: ServerConfig) -> list[dict[str, Any]]:
    path = review_queue.reviewed_path(config.queue_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _session_lines(config: ServerConfig) -> list[dict[str, Any]]:
    files = list(config.sessions_dir.glob("*_web.jsonl"))
    assert len(files) <= 1, "one session file per server run"
    if not files:
        return []
    return [json.loads(line) for line in files[0].read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# GET /api/queue — the DTO must never leak the mapping
# ---------------------------------------------------------------------------


def test_queue_lists_pending_most_important_first(api: Api) -> None:
    status, records, headers = api.get("/api/queue")
    assert status == 200
    assert [r["email"]["id"] for r in records] == ["<a@host>", "<b@host>", "<c@host>"]
    assert [r["record_id"] for r in records] == [RID_A, RID_B, RID_C]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"

    top = records[0]
    assert top["email"]["subject"] == "Hi"
    assert top["result"]["category"] == "needs_reply"
    assert top["decision"]["escalate"] is True
    assert top["draft"] == "Thanks Sarah — will do."
    assert top["provenance"] == "Claude"
    assert top["claude_used"] is True
    assert top["importance"] == 9.0
    assert top["source"] == "mbox:test.mbox"


def test_queue_response_has_no_mapping_or_headers(api: Api) -> None:
    review_queue.append_records(
        api.config.queue_dir,
        [
            _record(
                "<account@host>",
                draft="reply",
                source="imap",
                imap_account=ImapAccountRef(
                    host="imap.gmail.com",
                    user="private-account@gmail.com",
                    drafts_folder="[Gmail]/Drafts",
                ),
            )
        ],
    )
    status, records, _ = api.get("/api/queue")
    assert status == 200
    raw = json.dumps(records)
    assert "mapping" not in raw
    assert SECRET_PHONE not in raw  # the de-anonymization value itself
    assert "References" not in raw  # raw email headers are omitted too
    assert "private-account@gmail.com" not in raw
    assert records[0]["placeholder_count"] == 1
    assert records[1]["placeholder_count"] == 0


def test_queue_with_query_string_is_not_found(api: Api) -> None:
    status, _, _ = api.get("/api/queue?limit=1")
    assert status == 404


# ---------------------------------------------------------------------------
# POST /api/review — artifacts identical to the CLI path
# ---------------------------------------------------------------------------


def test_approve_writes_ledger_session_and_drafts(api: Api, tmp_path: Path) -> None:
    status, resp, _ = api.post(
        "/api/review",
        {
            "record_id": RID_A,
            "action": "approve",
            "draft": "Thanks Sarah — will do.",
        },
    )
    assert status == 200 and resp["ok"] is True
    txt_path = Path(resp["saved_path"])
    assert txt_path.exists() and txt_path.suffix == ".txt"

    # Byte-identical to the terminal review path (same shared function).
    expected = save_approved_draft(
        _processed_a(), "Thanks Sarah — will do.", tmp_path / "expected"
    )
    assert txt_path.read_text() == expected.read_text()

    # mbox source -> a .eml lands next to the .txt.
    assert resp["note"] and ".eml" in resp["note"]
    assert list(api.config.approved_dir.glob("*.eml"))

    reviewed = _reviewed_lines(api.config)
    assert len(reviewed) == 1
    assert reviewed[0]["record_id"] == RID_A
    assert reviewed[0]["email_id"] == "<a@host>"
    assert reviewed[0]["action"] == "approve"
    assert reviewed[0]["approved_path"] == str(txt_path)

    session = _session_lines(api.config)
    assert len(session) == 1
    rec = session[0]
    assert rec["email_id"] == "<a@host>"
    assert rec["action"] == "approve"
    assert rec["provenance"] == "Claude"
    assert rec["claude_used"] is True
    assert rec["num_placeholders"] == 1
    assert rec["approved_path"] == str(txt_path)

    # The reviewed email leaves the pending queue.
    _, records, _ = api.get("/api/queue")
    assert [r["email"]["id"] for r in records] == ["<b@host>", "<c@host>"]


def _processed_a() -> Any:
    from src.review_actions import processed_from_record

    return processed_from_record(
        _record(
            "<a@host>",
            draft="Thanks Sarah — will do.",
            importance=9.0,
            mapping={"Phone_F1": SECRET_PHONE},
        )
    )


def test_edit_persists_the_edited_draft(api: Api) -> None:
    status, resp, _ = api.post(
        "/api/review",
        {"record_id": RID_A, "action": "edit", "draft": "Edited reply text."},
    )
    assert status == 200
    content = Path(resp["saved_path"]).read_text()
    assert "Edited reply text." in content
    assert _reviewed_lines(api.config)[0]["action"] == "edit"


def test_reject_records_but_saves_nothing(api: Api) -> None:
    status, resp, _ = api.post(
        "/api/review", {"record_id": RID_B, "action": "reject", "draft": ""}
    )
    assert status == 200
    assert resp["saved_path"] is None
    assert not api.config.approved_dir.exists() or not list(
        api.config.approved_dir.iterdir()
    )
    reviewed = _reviewed_lines(api.config)
    assert reviewed[0]["action"] == "reject"
    assert reviewed[0]["approved_path"] is None
    assert _session_lines(api.config)[0]["action"] == "reject"


def test_legacy_imap_record_without_account_cannot_be_approved(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """<c@host> predates account metadata: approving it must not append into
    whatever IMAP account is configured now. Rejecting it still works."""
    monkeypatch.setenv("IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("IMAP_USER", "current@gmail.com")
    calls: list[bytes] = []
    monkeypatch.setattr(
        "src.review_actions.append_to_drafts", lambda raw, **kw: calls.append(raw)
    )
    status, resp, _ = api.post(
        "/api/review",
        {"record_id": RID_C, "action": "approve", "draft": "ok, sounds good"},
    )
    assert status == 409
    assert "reprocess" in resp["error"]
    assert calls == []
    assert _reviewed_lines(api.config) == []

    status, resp, _ = api.post(
        "/api/review", {"record_id": RID_C, "action": "reject", "draft": ""}
    )
    assert status == 200
    assert _reviewed_lines(api.config)[0]["record_id"] == RID_C


def test_imap_approval_uses_stored_account_and_folder(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = ImapAccountRef(
        host="imap.gmail.com",
        user="me@gmail.com",
        drafts_folder="[Gmail]/Drafts",
    )
    review_queue.append_records(
        api.config.queue_dir,
        [_record("<gmail@host>", draft="reply", source="imap", imap_account=account)],
    )
    monkeypatch.setenv("IMAP_HOST", account.host)
    monkeypatch.setenv("IMAP_USER", account.user)
    calls: list[tuple[bytes, str | None]] = []

    def _append(raw: bytes, *, folder: str | None = None) -> None:
        calls.append((raw, folder))

    monkeypatch.setattr("src.review_actions.append_to_drafts", _append)
    status, resp, _ = api.post(
        "/api/review",
        {
            "record_id": compute_record_id("imap", account, "<gmail@host>"),
            "action": "approve",
            "draft": "reply",
        },
    )
    assert status == 200
    assert resp["note"] == "saved to IMAP Drafts (not sent)"
    assert len(calls) == 1
    assert calls[0][1] == "[Gmail]/Drafts"
    assert b"From: me@gmail.com" in calls[0][0]  # the stored account, not env
    assert not list(api.config.approved_dir.glob("*.eml"))  # no .eml for imap


def test_same_message_id_in_two_accounts_reviews_independently(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same Message-ID delivered to two IMAP accounts is two queue
    records; reviewing one leaves the other pending and routable."""
    account_one = ImapAccountRef(
        host="imap.gmail.com", user="one@gmail.com", drafts_folder="[Gmail]/Drafts"
    )
    account_two = ImapAccountRef(
        host="imap.gmail.com", user="two@gmail.com", drafts_folder="[Gmail]/Drafts"
    )
    review_queue.append_records(
        api.config.queue_dir,
        [
            _record("<dup@host>", draft="r1", source="imap", imap_account=account_one),
            _record("<dup@host>", draft="r2", source="imap", imap_account=account_two),
        ],
    )
    rid_one = compute_record_id("imap", account_one, "<dup@host>")
    rid_two = compute_record_id("imap", account_two, "<dup@host>")
    assert rid_one != rid_two

    _, records, _ = api.get("/api/queue")
    ids = [r["record_id"] for r in records]
    assert rid_one in ids and rid_two in ids

    status, _, _ = api.post(
        "/api/review", {"record_id": rid_one, "action": "reject", "draft": ""}
    )
    assert status == 200
    _, records, _ = api.get("/api/queue")
    ids = [r["record_id"] for r in records]
    assert rid_one not in ids and rid_two in ids


def test_imap_approval_blocks_the_wrong_active_account(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = ImapAccountRef(
        host="imap.gmail.com",
        user="original@gmail.com",
        drafts_folder="[Gmail]/Drafts",
    )
    review_queue.append_records(
        api.config.queue_dir,
        [_record("<wrong@host>", draft="reply", source="imap", imap_account=account)],
    )
    monkeypatch.setenv("IMAP_HOST", account.host)
    monkeypatch.setenv("IMAP_USER", "other@gmail.com")
    status, resp, _ = api.post(
        "/api/review",
        {
            "record_id": compute_record_id("imap", account, "<wrong@host>"),
            "action": "approve",
            "draft": "reply",
        },
    )
    assert status == 409
    assert "different IMAP account" in resp["error"]
    assert "<wrong@host>" not in {
        row["email_id"] for row in _reviewed_lines(api.config)
    }
    assert not list(api.config.approved_dir.glob("*wrong*"))


def test_two_reviews_share_one_session_file(api: Api) -> None:
    api.post("/api/review", {"record_id": RID_A, "action": "reject", "draft": ""})
    api.post("/api/review", {"record_id": RID_B, "action": "reject", "draft": ""})
    assert len(_session_lines(api.config)) == 2  # helper asserts a single file


def test_review_validation_rejects_bad_input(api: Api) -> None:
    cases: list[Any] = [
        {"record_id": "not-a-known-id", "action": "approve", "draft": "x"},
        {"email_id": "<a@host>", "action": "approve", "draft": "x"},  # legacy key
        {"record_id": RID_A, "action": "send", "draft": "x"},  # bad action
        {"record_id": RID_A, "action": "approve", "draft": ""},  # empty draft
        {
            "record_id": RID_B,
            "action": "approve",
            "draft": "typed",
        },  # record has no draft
        {"record_id": RID_A, "action": "approve", "draft": "x" * 100_001},
        ["not", "a", "dict"],
    ]
    for body in cases:
        status, resp, _ = api.post("/api/review", body)
        assert status == 400, body
        assert "error" in resp

    status, _, _ = api.request(
        "POST", "/api/review", raw_body=b"{not json", content_type="application/json"
    )
    assert status == 400

    # Nothing was persisted by any of the failures.
    assert _reviewed_lines(api.config) == []
    assert _session_lines(api.config) == []


# ---------------------------------------------------------------------------
# The security gate
# ---------------------------------------------------------------------------


def test_missing_or_wrong_token_is_403(api: Api) -> None:
    status, _, _ = api.get("/api/queue", token=None)
    assert status == 403
    status, _, _ = api.get("/api/queue", token="wrong-token")
    assert status == 403
    status, resp, _ = api.post(
        "/api/review",
        {"record_id": RID_A, "action": "approve", "draft": "x"},
        token=None,
    )
    assert status == 403
    assert _reviewed_lines(api.config) == []


def test_dns_rebinding_host_is_403(api: Api) -> None:
    for bad_host in (
        f"evil.example:{api.port}",  # rebound hostname
        "127.0.0.1:9999",  # wrong port
        "127.0.0.1",  # missing port — not an exact match
    ):
        status, _, _ = api.get("/api/queue", host=bad_host)
        assert status == 403, bad_host
    status, _, _ = api.get("/api/queue", host=f"localhost:{api.port}")
    assert status == 200


def test_cross_origin_post_is_403(api: Api) -> None:
    status, _, _ = api.post(
        "/api/review",
        {"record_id": RID_A, "action": "approve", "draft": "x"},
        origin="https://evil.example",
    )
    assert status == 403
    assert _reviewed_lines(api.config) == []

    # The Vite dev origin is allowed (bad body proves the gate was passed).
    status, _, _ = api.post("/api/review", {"bad": 1}, origin="http://localhost:5173")
    assert status == 400


def test_missing_origin_post_is_403(api: Api) -> None:
    status, _, _ = api.post("/api/review", {"bad": 1}, origin=None)
    assert status == 403


def test_get_with_foreign_origin_is_still_served(api: Api) -> None:
    # Origin allowlisting applies to mutating requests; GETs are gated by
    # host + token (a foreign page can't have the token anyway).
    status, _, _ = api.get("/api/queue", origin="https://evil.example")
    assert status == 200


def test_post_content_type_and_size_limits(api: Api) -> None:
    status, _, _ = api.post(
        "/api/review",
        {"record_id": RID_A, "action": "approve", "draft": "x"},
        content_type="text/plain",
    )
    assert status == 415

    # The 413 is sent from the declared Content-Length alone — the server
    # never reads the oversized body, so speak raw HTTP and skip sending it
    # (http.client would die on EPIPE mid-upload, which is the correct
    # server-side behavior: don't swallow a body you've already refused).
    with socket.create_connection(("127.0.0.1", api.port), timeout=10) as sock:
        sock.sendall(
            (
                f"POST /api/review HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{api.port}\r\n"
                f"X-Triage-Token: {api.token}\r\n"
                f"Origin: http://localhost:5173\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {1024 * 1024 + 100}\r\n"
                f"\r\n"
            ).encode()
        )
        status_line = sock.recv(65536).decode(errors="replace").splitlines()[0]
    assert " 413 " in status_line


def test_unknown_paths_and_methods(api: Api) -> None:
    status, _, _ = api.get("/api/nope")
    assert status == 404
    status, _, _ = api.post("/api/nope", {})
    assert status == 404
    status, _, _ = api.request("PUT", "/api/queue")
    assert status == 405
    # Errors carry the same hygiene headers as every other response.
    _, _, headers = api.get("/api/nope")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in headers


def test_token_file_is_0600_and_matches(api: Api) -> None:
    token_path = api.config.token_path
    assert token_path.read_text().strip() == api.token
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# Background processing — the web invokes the shared CLI pipeline
# ---------------------------------------------------------------------------


def _wait_for_job(api: Api, expected: str) -> dict[str, Any]:
    for _ in range(200):
        status, job, _ = api.get("/api/process/status")
        assert status == 200
        if job["status"] == expected:
            return job
        threading.Event().wait(0.01)
    raise AssertionError(f"processing job never reached {expected!r}")


def test_process_job_runs_shared_pipeline_and_rejects_overlap(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[ServerConfig, str, int, int | None, str, str]] = []

    def _fake_run(config: ServerConfig, job: Any) -> int:
        calls.append(
            (config, job.source, job.days, job.limit, job.anonymizer, job.task)
        )
        started.set()
        assert release.wait(timeout=5)
        return 0

    monkeypatch.setattr("src.api.server._run_pipeline", _fake_run)
    status, idle, _ = api.get("/api/process/status")
    assert status == 200 and idle["status"] == "idle"

    status, job, _ = api.post(
        "/api/process",
        {
            "source": "imap",
            "days": 14,
            "limit": 25,
            "anonymizer": "regex",
            "task": "Summarize instead of replying.",
        },
    )
    assert status == 202
    assert started.wait(timeout=2)
    assert job["source"] == "imap"
    running = _wait_for_job(api, "running")
    assert running["days"] == 14
    assert running["limit"] == 25
    assert running["anonymizer"] == "regex"

    status, resp, _ = api.post("/api/process", {"source": "mbox"})
    assert status == 409
    assert "already running" in resp["error"]

    status, resp, _ = api.post(
        "/api/settings/imap",
        {
            "host": "imap.gmail.com",
            "user": "me@gmail.com",
            "password": "app-password",
            "folder": "INBOX",
            "drafts_folder": "[Gmail]/Drafts",
        },
    )
    assert status == 409
    assert "while processing" in resp["error"]

    release.set()
    completed = _wait_for_job(api, "succeeded")
    assert completed["exit_code"] == 0
    assert calls == [
        (api.config, "imap", 14, 25, "regex", "Summarize instead of replying.")
    ]


def test_process_job_reports_failure_without_leaking_exception(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(config: ServerConfig, job: Any) -> int:
        raise RuntimeError("secret model detail")

    monkeypatch.setattr("src.api.server._run_pipeline", _fail)
    status, _, _ = api.post("/api/process", {"source": "mbox"})
    assert status == 202
    failed = _wait_for_job(api, "failed")
    assert failed["exit_code"] == 1
    assert "secret model detail" not in json.dumps(failed)


def test_process_request_validation(api: Api) -> None:
    for body in (
        {},
        {"source": "smtp"},
        {"source": "imap", "days": 0},
        {"source": "imap", "days": True},
        {"source": "imap", "days": 366},
        {"source": "imap", "limit": 0},
        {"source": "imap", "limit": True},
        {"source": "imap", "limit": 10_001},
        {"source": "imap", "limit": "10"},
        {"source": "imap", "anonymizer": "none"},  # only the fixed allowlist
        {"source": "imap", "anonymizer": "../configs/evil.yaml"},
        {"source": "imap", "task": "line one\nline two"},  # control characters
        {"source": "imap", "task": "x" * 2_001},
        {"source": "imap", "task": 42},
    ):
        status, resp, _ = api.post("/api/process", body)
        assert status == 400, body
        assert "error" in resp


def test_process_defaults_match_the_cli(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Any] = []

    def _fake_run(config: ServerConfig, job: Any) -> int:
        captured.append(job)
        return 0

    monkeypatch.setattr("src.api.server._run_pipeline", _fake_run)
    status, _, _ = api.post("/api/process", {"source": "mbox"})
    assert status == 202
    _wait_for_job(api, "succeeded")
    (job,) = captured
    assert (job.limit, job.anonymizer, job.task) == (None, "combined", "")


# ---------------------------------------------------------------------------
# POST /api/reset — the web mirror of the CLI `reset` command
# ---------------------------------------------------------------------------


def test_reset_deletes_only_the_queue_ledgers(api: Api) -> None:
    # An approved draft and a session log must survive a reset.
    api.post(
        "/api/review",
        {"record_id": RID_A, "action": "approve", "draft": "Thanks Sarah — will do."},
    )
    assert list(api.config.approved_dir.glob("*.txt"))
    assert _session_lines(api.config)

    status, resp, _ = api.post("/api/reset", {})
    assert status == 200
    assert resp["ok"] is True
    assert resp["processed_deleted"] == 3
    assert resp["reviewed_deleted"] == 1
    assert not review_queue.processed_path(api.config.queue_dir).exists()
    assert not review_queue.reviewed_path(api.config.queue_dir).exists()
    assert list(api.config.approved_dir.glob("*.txt"))  # drafts kept
    assert _session_lines(api.config)  # session log kept

    _, records, _ = api.get("/api/queue")
    assert records == []

    # Resetting an already-empty queue is a no-op, not an error.
    status, resp, _ = api.post("/api/reset", {})
    assert status == 200 and resp["processed_deleted"] == 0


def test_reset_refused_while_processing(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def _fake_run(config: ServerConfig, job: Any) -> int:
        assert release.wait(timeout=5)
        return 0

    monkeypatch.setattr("src.api.server._run_pipeline", _fake_run)
    status, _, _ = api.post("/api/process", {"source": "mbox"})
    assert status == 202
    _wait_for_job(api, "running")

    status, resp, _ = api.post("/api/reset", {})
    assert status == 409
    assert "while processing" in resp["error"]
    assert review_queue.processed_path(api.config.queue_dir).exists()

    release.set()
    _wait_for_job(api, "succeeded")


def test_reset_requires_the_gate(api: Api) -> None:
    status, _, _ = api.post("/api/reset", {}, token=None)
    assert status == 403
    status, _, _ = api.post("/api/reset", {}, origin="https://evil.example")
    assert status == 403
    assert review_queue.processed_path(api.config.queue_dir).exists()


# ---------------------------------------------------------------------------
# IMAP settings — .env round-trip
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_imap_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Sandbox the IMAP_* vars the handlers read and write via os.environ."""
    for key in (
        "IMAP_HOST",
        "IMAP_USER",
        "IMAP_PASS",
        "IMAP_FOLDER",
        "IMAP_DRAFTS_FOLDER",
    ):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    return monkeypatch


def test_settings_get_reports_password_presence_only(
    api: Api, clean_imap_env: pytest.MonkeyPatch
) -> None:
    status, resp, _ = api.get("/api/settings/imap")
    assert status == 200
    assert resp == {
        "host": "",
        "user": "",
        "folder": "INBOX",
        "drafts_folder": "Drafts",
        "password": "unset",
    }

    clean_imap_env.setenv("IMAP_HOST", "imap.example.com")
    clean_imap_env.setenv("IMAP_PASS", "hunter2-app-password")
    status, resp, _ = api.get("/api/settings/imap")
    assert resp["host"] == "imap.example.com"
    assert resp["password"] == "set"
    assert "hunter2-app-password" not in json.dumps(resp)


def test_provider_drafts_folder_defaults() -> None:
    assert default_drafts_folder("imap.gmail.com") == "[Gmail]/Drafts"
    assert default_drafts_folder("imap.mail.yahoo.com") == "Draft"
    assert default_drafts_folder("outlook.office365.com") == "Drafts"


def test_settings_post_round_trips_env_file(
    api: Api, clean_imap_env: pytest.MonkeyPatch
) -> None:
    env_path = api.config.env_path
    env_path.write_text(
        "# keep this comment\n"
        "ANTHROPIC_API_KEY=sk-test-123\n"
        "IMAP_HOST=old.example.com\n"
        "UNRELATED=1\n"
    )
    env_path.chmod(0o644)

    status, resp, _ = api.post(
        "/api/settings/imap",
        {
            "host": "imap.gmail.com",
            "user": "me@gmail.com",
            "password": "abcd efgh ijkl mnop",
            "folder": "INBOX",
        },
    )
    assert status == 200 and resp["ok"] is True and resp["password"] == "set"

    text = env_path.read_text()
    lines = text.splitlines()
    assert "# keep this comment" in lines  # comments preserved
    assert "ANTHROPIC_API_KEY=sk-test-123" in lines  # unrelated keys preserved
    assert "UNRELATED=1" in lines
    assert "IMAP_HOST=imap.gmail.com" in lines  # updated in place
    assert lines.index("IMAP_HOST=imap.gmail.com") == 2
    assert "IMAP_USER=me@gmail.com" in lines
    assert "IMAP_PASS=abcd efgh ijkl mnop" in lines
    assert "IMAP_FOLDER=INBOX" in lines
    assert "IMAP_DRAFTS_FOLDER=[Gmail]/Drafts" in lines
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    # os.environ reflects the save immediately.
    assert os.environ["IMAP_HOST"] == "imap.gmail.com"

    # Saving again with an empty password keeps the stored one.
    status, resp, _ = api.post(
        "/api/settings/imap",
        {
            "host": "imap.gmail.com",
            "user": "me@gmail.com",
            "password": "",
            "folder": "INBOX",
        },
    )
    assert resp["password"] == "set"
    assert "IMAP_PASS=abcd efgh ijkl mnop" in env_path.read_text()


def test_settings_post_rejects_env_injection(
    api: Api, clean_imap_env: pytest.MonkeyPatch
) -> None:
    env_path = api.config.env_path
    env_path.write_text("ANTHROPIC_API_KEY=sk-test-123\n")
    before = env_path.read_text()

    bad_values = [
        {
            "host": "imap.gmail.com\nEVIL=1",
            "user": "u",
            "password": "p",
            "folder": "INBOX",
        },
        {
            "host": "imap.gmail.com",
            "user": "u\r\nEVIL=1",
            "password": "p",
            "folder": "INBOX",
        },
        {
            "host": "imap.gmail.com",
            "user": "u",
            "password": "p\nEVIL=1",
            "folder": "INBOX",
        },
        {"host": "imap.gmail.com", "user": "u", "password": "p", "folder": "INBOX\x00"},
        {"host": "not a hostname!", "user": "u", "password": "p", "folder": "INBOX"},
        {"host": "", "user": "u", "password": "p", "folder": "INBOX"},
        {"host": "h" * 300 + ".com", "user": "u", "password": "p", "folder": "INBOX"},
    ]
    for body in bad_values:
        status, resp, _ = api.post("/api/settings/imap", body)
        assert status == 400, body
        assert "EVIL" not in env_path.read_text()
    assert env_path.read_text() == before  # nothing was written


def test_write_env_keys_creates_missing_file_and_drops_duplicates(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    write_env_keys(env_path, {"IMAP_HOST": "a.example.com"})
    assert env_path.read_text() == "IMAP_HOST=a.example.com\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    env_path.write_text("IMAP_HOST=one\nOTHER=2\nIMAP_HOST=two\n")
    write_env_keys(env_path, {"IMAP_HOST": "final.example.com"})
    text = env_path.read_text()
    assert text.count("IMAP_HOST") == 1  # the stale duplicate is dropped
    assert "IMAP_HOST=final.example.com" in text
    assert "OTHER=2" in text

    with pytest.raises(ValueError):
        write_env_keys(env_path, {"IMAP_HOST": "evil\ninjected=1"})
    with pytest.raises(ValueError):
        write_env_keys(env_path, {"lowercase key": "x"})


# ---------------------------------------------------------------------------
# IMAP connection test endpoint — mocked imaplib, no network
# ---------------------------------------------------------------------------


class _FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL; records ctor args, scripts login."""

    instances: list["_FakeIMAP"] = []
    login_error: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.logged_out = False
        self.selected: list[tuple[str, bool]] = []
        _FakeIMAP.instances.append(self)

    def login(self, user: str, password: str) -> None:
        if _FakeIMAP.login_error:
            raise imaplib.IMAP4.error(_FakeIMAP.login_error)

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly is True  # the test endpoint must never open read-write
        self.selected.append((folder, readonly))
        return "OK", [b"7"]

    def logout(self) -> None:
        self.logged_out = True


@pytest.fixture()
def fake_imap(monkeypatch: pytest.MonkeyPatch) -> type[_FakeIMAP]:
    _FakeIMAP.instances = []
    _FakeIMAP.login_error = None
    monkeypatch.setattr("src.api.server.imaplib.IMAP4_SSL", _FakeIMAP)
    return _FakeIMAP


def test_imap_test_success(
    api: Api, clean_imap_env: pytest.MonkeyPatch, fake_imap: type[_FakeIMAP]
) -> None:
    status, resp, _ = api.post(
        "/api/settings/imap/test",
        {
            "host": "imap.gmail.com",
            "user": "me@gmail.com",
            "password": "pw",
            "folder": "INBOX",
        },
    )
    assert status == 200
    assert resp["ok"] is True
    assert "7 message(s)" in resp["message"]

    (instance,) = fake_imap.instances
    assert instance.args == ("imap.gmail.com",)
    # Default certificate verification: no custom/unverified SSL context.
    assert "ssl_context" not in instance.kwargs
    assert set(instance.kwargs) <= {"timeout"}
    assert instance.selected == [("INBOX", True), ("[Gmail]/Drafts", True)]
    assert instance.logged_out


def test_imap_test_login_failure_never_echoes_credentials(
    api: Api, clean_imap_env: pytest.MonkeyPatch, fake_imap: type[_FakeIMAP]
) -> None:
    fake_imap.login_error = (
        "[AUTHENTICATIONFAILED] invalid credentials for me@gmail.com"
    )
    status, resp, _ = api.post(
        "/api/settings/imap/test",
        {
            "host": "imap.gmail.com",
            "user": "me@gmail.com",
            "password": "sekrit-pass",
            "folder": "INBOX",
        },
    )
    assert status == 200
    assert resp["ok"] is False
    assert "login failed" in resp["error"]
    assert "me@gmail.com" not in json.dumps(resp)
    assert "sekrit-pass" not in json.dumps(resp)


def test_imap_test_falls_back_to_saved_env(
    api: Api, clean_imap_env: pytest.MonkeyPatch, fake_imap: type[_FakeIMAP]
) -> None:
    clean_imap_env.setenv("IMAP_HOST", "imap.example.com")
    clean_imap_env.setenv("IMAP_USER", "stored@example.com")
    clean_imap_env.setenv("IMAP_PASS", "stored-pass")
    status, resp, _ = api.post("/api/settings/imap/test", {})
    assert status == 200 and resp["ok"] is True
    assert fake_imap.instances[0].args == ("imap.example.com",)


def test_imap_test_reports_missing_fields(
    api: Api, clean_imap_env: pytest.MonkeyPatch, fake_imap: type[_FakeIMAP]
) -> None:
    status, resp, _ = api.post("/api/settings/imap/test", {})
    assert status == 200
    assert resp["ok"] is False
    assert "missing" in resp["error"]
    assert fake_imap.instances == []  # no connection was even attempted


# ---------------------------------------------------------------------------
# POST /api/import-mbox — native selection, validated local copy
# ---------------------------------------------------------------------------


def test_import_mbox_copies_native_selection_and_ignores_client_path(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "selected.mbox"
    source.write_bytes(b"From sender@example.com\nSubject: hello\n\nbody\n")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class _Done:
        returncode = 0
        stdout = f"{source}\n"

    def _fake_run(argv: list[str], **kw: Any) -> _Done:
        calls.append((list(argv), kw))
        return _Done()

    monkeypatch.setattr("src.api.server.subprocess.run", _fake_run)
    status, resp, _ = api.post(
        "/api/import-mbox", {"path": "/etc/passwd", "cmd": "ignored"}
    )
    assert status == 200
    assert resp == {
        "ok": True,
        "cancelled": False,
        "path": str(api.config.inbox_dir / "selected.mbox"),
        "filename": "selected.mbox",
    }
    assert (api.config.inbox_dir / "selected.mbox").read_bytes() == source.read_bytes()
    argv, kwargs = calls[0]
    assert argv[:2] == ["osascript", "-e"]
    assert "choose file" in argv[2]
    assert "/etc/passwd" not in argv[2]
    assert kwargs == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 300,
    }


def test_import_mbox_cancel_is_a_noop(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Cancelled:
        returncode = 0
        stdout = "\n"

    monkeypatch.setattr(
        "src.api.server.subprocess.run", lambda *args, **kwargs: _Cancelled()
    )
    status, resp, _ = api.post("/api/import-mbox", {})
    assert status == 200
    assert resp == {"ok": True, "cancelled": True, "path": None}
    assert not api.config.inbox_dir.exists()


def test_import_mbox_rejects_non_mbox_selection(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("not an mbox")

    class _Done:
        returncode = 0
        stdout = f"{source}\n"

    monkeypatch.setattr("src.api.server.subprocess.run", lambda *a, **kw: _Done())
    status, resp, _ = api.post("/api/import-mbox", {})
    assert status == 400
    assert resp["error"] == "selected file must end in .mbox"
    assert not api.config.inbox_dir.exists()


def test_import_mbox_preserves_existing_file_with_numeric_suffix(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "mail.mbox"
    source.write_bytes(b"new")
    api.config.inbox_dir.mkdir()
    (api.config.inbox_dir / "mail.mbox").write_bytes(b"existing")

    class _Done:
        returncode = 0
        stdout = f"{source}\n"

    monkeypatch.setattr("src.api.server.subprocess.run", lambda *a, **kw: _Done())
    status, resp, _ = api.post("/api/import-mbox", {})
    assert status == 200
    assert resp["filename"] == "mail-2.mbox"
    assert (api.config.inbox_dir / "mail.mbox").read_bytes() == b"existing"
    assert (api.config.inbox_dir / "mail-2.mbox").read_bytes() == b"new"


def test_import_mbox_reports_picker_failure(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr("src.api.server.subprocess.run", lambda *a, **kw: _Failed())
    status, resp, _ = api.post("/api/import-mbox", {})
    assert status == 500
    assert resp["error"] == "could not open the .mbox file picker"
