"""Persistent processing queue bridging the `start` and `review` commands.

`start` triages (and, for escalations, delegates) emails up front and appends
one JSON line per finished email to ``<queue-dir>/processed.jsonl``; `review`
later walks every record that has no entry in ``<queue-dir>/reviewed.jsonl``.
Both files are append-only — state is derived by replaying them, never by
rewriting in place — so an interrupted run can at worst redo work, not
corrupt it. The records hold raw email bodies and placeholder mappings, which
is why the queue lives under ``data/`` (gitignored): nothing sensitive ever
leaves the box or enters version control.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.ingestion.mbox_loader import Email
from src.router.sensitivity_scorer import EscalationDecision
from src.triage.classifier import TriageResult

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_DIR = Path("data/queue")

PROCESSED_FILENAME = "processed.jsonl"
REVIEWED_FILENAME = "reviewed.jsonl"


def source_is_imap(source: str) -> bool:
    """True when an email's source string names an IMAP connection.

    IMAP sources are ``"imap"`` (both ``start-imap`` and ``process --source
    imap``); mbox sources are ``"mbox:<file>"`` (``start``) or ``"mbox"``
    (``process``). An empty/unknown source is treated as mbox.
    """
    return source.strip().lower().startswith("imap")


def compute_record_id(
    source: str, imap_account: "ImapAccountRef | None", email_id: str
) -> str:
    """Opaque queue identity: the Message-ID scoped by where it came from.

    A raw Message-ID is not a safe ledger key — the same message delivered to
    two IMAP accounts carries the same Message-ID, and keying on it alone lets
    one account's processing (or review) suppress the other's. The scope is:

    - mbox sources share one ``mbox`` scope, preserving cross-file dedupe
      (the same export dropped in twice is still one email);
    - account-scoped IMAP records hash in the normalized host and username,
      so identical Message-IDs in different accounts stay distinct;
    - IMAP records without stored account metadata (legacy ledger lines) get
      a bare ``imap`` scope — deliberately different from every account
      scope, so an old review can never suppress a newly ingested
      account-scoped record.

    The hash keeps the id opaque: it can appear in API responses and UI keys
    without carrying the account username along.
    """
    if source_is_imap(source):
        if imap_account is not None:
            scope = (
                "imap",
                imap_account.host.strip().lower(),
                imap_account.user.strip().lower(),
            )
        else:
            scope = ("imap",)
    else:
        scope = ("mbox",)
    payload = "\x1f".join((*scope, email_id))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ImapAccountRef:
    """Non-secret IMAP routing metadata captured when an email is ingested."""

    host: str
    user: str
    drafts_folder: str

    @classmethod
    def from_json_dict(cls, d: Any) -> "ImapAccountRef":
        if not isinstance(d, dict):
            raise ValueError("'imap_account' must be a dict or null")
        values: dict[str, str] = {}
        for key in ("host", "user", "drafts_folder"):
            value = d.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"'imap_account.{key}' must be a non-empty string")
            if len(value) > 320 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError(f"'imap_account.{key}' is invalid")
            values[key] = value.strip()
        return cls(**values)


@dataclass
class QueueRecord:
    """One fully processed email waiting for (or done with) human review."""

    email: Email
    result: TriageResult
    decision: EscalationDecision
    draft: str | None
    provenance: str  # "local" or "Claude"
    mapping: dict[str, str]  # placeholder -> original (empty unless escalated)
    claude_used: bool
    error: str | None
    importance: float  # 1 (ignore) .. 10 (urgent)
    importance_reason: str
    ranked_by: str  # "Claude" or a fallback description
    source: str  # e.g. "mbox:enron_50.mbox" or "imap"
    processed_at: str  # ISO timestamp
    imap_account: ImapAccountRef | None = None
    # Opaque, source-scoped ledger identity; derived when absent so records
    # written before the field existed keep loading (see compute_record_id).
    record_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = compute_record_id(
                self.source, self.imap_account, self.email.id
            )

    def to_json_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["email"]["date"] = self.email.date.isoformat()
        return d

    @classmethod
    def from_json_dict(cls, d: dict[str, Any]) -> "QueueRecord":
        """Validate one parsed ``processed.jsonl`` line. Raises ValueError."""
        if not isinstance(d, dict):
            raise ValueError(f"Expected a dict, got {type(d).__name__}")
        for key in ("email", "result", "decision"):
            if key not in d or not isinstance(d[key], dict):
                raise ValueError(f"Missing or non-dict field: {key!r}")

        email = _email_from_dict(d["email"])
        result = TriageResult.from_json_dict(d["result"])

        dec = d["decision"]
        if not isinstance(dec.get("escalate"), bool):
            raise ValueError("'decision.escalate' must be a bool")
        decision = EscalationDecision(
            escalate=dec["escalate"],
            reason=str(dec.get("reason", "")),
            score=float(dec.get("score", 0.0)),
        )

        mapping = d.get("mapping") or {}
        if not isinstance(mapping, dict):
            raise ValueError("'mapping' must be a dict")

        importance = d.get("importance", 1.0)
        if isinstance(importance, bool) or not isinstance(importance, (int, float)):
            raise ValueError("'importance' must be a number")

        raw_imap_account = d.get("imap_account")
        imap_account = (
            ImapAccountRef.from_json_dict(raw_imap_account)
            if raw_imap_account is not None
            else None
        )

        return cls(
            email=email,
            result=result,
            decision=decision,
            draft=d.get("draft"),
            provenance=str(d.get("provenance", "local")),
            mapping={str(k): str(v) for k, v in mapping.items()},
            claude_used=bool(d.get("claude_used", False)),
            error=d.get("error"),
            importance=float(importance),
            importance_reason=str(d.get("importance_reason", "")),
            ranked_by=str(d.get("ranked_by", "")),
            source=str(d.get("source", "")),
            processed_at=str(d.get("processed_at", "")),
            imap_account=imap_account,
            record_id=str(d.get("record_id") or ""),
        )


def _email_from_dict(d: dict[str, Any]) -> Email:
    for key in ("id", "from_addr", "subject", "body_plain"):
        if key not in d:
            raise ValueError(f"Missing required email field: {key!r}")
    try:
        date = datetime.fromisoformat(d.get("date", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bad email date {d.get('date')!r}: {exc}") from exc
    return Email(
        id=str(d["id"]),
        from_addr=str(d["from_addr"]),
        to_addrs=[str(a) for a in d.get("to_addrs", [])],
        subject=str(d["subject"]),
        date=date,
        body_plain=str(d["body_plain"]),
        thread_id=d.get("thread_id"),
        headers={str(k): str(v) for k, v in (d.get("headers") or {}).items()},
    )


# ---------------------------------------------------------------------------
# Ledger access
# ---------------------------------------------------------------------------


def processed_path(queue_dir: Path) -> Path:
    return queue_dir / PROCESSED_FILENAME


def reviewed_path(queue_dir: Path) -> Path:
    return queue_dir / REVIEWED_FILENAME


def append_records(queue_dir: Path, records: list[QueueRecord]) -> None:
    if not records:
        return
    path = processed_path(queue_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record.to_json_dict(), ensure_ascii=False) + "\n")


def load_records(queue_dir: Path) -> list[QueueRecord]:
    """All processed records, in file order. Malformed lines warn and skip."""
    path = processed_path(queue_dir)
    if not path.exists():
        return []
    records: list[QueueRecord] = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(QueueRecord.from_json_dict(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("%s:%d: skipping bad record (%s)", path, lineno, exc)
    return records


def processed_record_ids(queue_dir: Path) -> set[str]:
    return {record.record_id for record in load_records(queue_dir)}


def append_reviewed(
    queue_dir: Path,
    record_id: str,
    email_id: str,
    action: str,
    approved_path: Path | None,
) -> None:
    path = reviewed_path(queue_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "record_id": record_id,
        "email_id": email_id,
        "action": action,
        "approved_path": str(approved_path) if approved_path else None,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


@dataclass(frozen=True)
class ReviewedKeys:
    """The reviewed ledger split by entry vintage.

    Entries written since record ids existed carry ``record_id``; older ones
    carry only the raw ``email_id``. Legacy keys must only ever suppress
    records that are themselves unscoped (mbox or legacy IMAP) — matching a
    raw Message-ID against an account-scoped record would let a review from
    one account swallow the same message in another.
    """

    record_ids: set[str]
    legacy_email_ids: set[str]


def reviewed_keys(queue_dir: Path) -> ReviewedKeys:
    path = reviewed_path(queue_dir)
    record_ids: set[str] = set()
    legacy_email_ids: set[str] = set()
    if not path.exists():
        return ReviewedKeys(record_ids, legacy_email_ids)
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                record_id = entry.get("record_id")
                if isinstance(record_id, str) and record_id:
                    record_ids.add(record_id)
                else:
                    legacy_email_ids.add(str(entry["email_id"]))
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("%s: skipping bad reviewed entry", path)
    return ReviewedKeys(record_ids, legacy_email_ids)


def reviewed_ids(queue_dir: Path) -> set[str]:
    """Every reviewed email id, old and new entries alike (used for counts)."""
    path = reviewed_path(queue_dir)
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(str(json.loads(line)["email_id"]))
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("%s: skipping bad reviewed entry", path)
    return ids


def pending_records(queue_dir: Path) -> list[QueueRecord]:
    """Unreviewed records, most important first (ties: oldest processed first).

    Deduped by record id keeping the first occurrence — a reprocessed email
    shouldn't show up for review twice, but the same Message-ID ingested from
    two different IMAP accounts is two distinct records.
    """
    done = reviewed_keys(queue_dir)
    seen: set[str] = set()
    pending: list[QueueRecord] = []
    for record in load_records(queue_dir):
        if record.record_id in done.record_ids or record.record_id in seen:
            continue
        # Pre-record_id reviewed entries only suppress unscoped records.
        if record.imap_account is None and record.email.id in done.legacy_email_ids:
            continue
        seen.add(record.record_id)
        pending.append(record)
    pending.sort(key=lambda r: (-r.importance, r.processed_at))
    return pending
