"""The persistence half of review: what happens after a human decides.

Both review surfaces — the terminal `review` command and the web UI's
``POST /api/review`` — funnel through this module, so an approval produces
byte-identical artifacts no matter where it was clicked: the ``.txt`` draft
under ``data/approved_drafts/``, the routed copy (``.eml`` for mbox sources,
an IMAP Drafts APPEND for imap sources), and the session-log record. The
functions here never print; callers decide how to surface the returned notes
(rich console vs. JSON response). Nothing in this module ever sends mail —
the strongest action is leaving a draft where the user can send it themselves.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from src import review_queue
from src.ingestion.imap_loader import append_to_drafts
from src.ingestion.mbox_loader import Email
from src.router.sensitivity_scorer import EscalationDecision
from src.triage.classifier import TriageResult

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ProcessedEmail:
    """One email after triage + (optional) escalation, ready to present."""

    email: Email
    result: TriageResult
    decision: EscalationDecision
    draft: str | None
    provenance: str  # "local" or "Claude"
    mapping: dict[str, str]  # placeholder -> original (empty unless escalated)
    claude_used: bool
    error: str | None  # escalation-path error note, if any


@dataclass
class PersistOutcome:
    """What :func:`persist_approved` did, for the caller to report.

    ``txt_path`` always points at the written ``.txt`` draft. Exactly one of
    ``note`` (the best-effort routed step succeeded) or ``warning`` (it
    failed, review continues anyway) is set.
    """

    txt_path: Path
    note: str | None
    warning: str | None


class ImapAccountMismatchError(RuntimeError):
    """The queued email belongs to a different IMAP account than the active one."""


def processed_from_record(rec: review_queue.QueueRecord) -> ProcessedEmail:
    """Rebuild the presentation-ready ProcessedEmail from a stored queue record."""
    return ProcessedEmail(
        email=rec.email,
        result=rec.result,
        decision=rec.decision,
        draft=rec.draft,
        provenance=rec.provenance,
        mapping=rec.mapping,
        claude_used=rec.claude_used,
        error=rec.error,
    )


def safe_stem(email: Email) -> str:
    raw = (email.id or "email").strip().strip("<>")
    stem = _UNSAFE_FILENAME_RE.sub("_", raw).strip("_")
    return (stem or "email")[:80]


def save_approved_draft(p: ProcessedEmail, draft: str, out_dir: Path) -> Path:
    """Write an approved draft to ``out_dir``; never overwrites an existing file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(p.email)
    path = out_dir / f"{stem}.txt"
    n = 2
    while path.exists():
        path = out_dir / f"{stem}-{n}.txt"
        n += 1
    header = (
        f"To: {p.email.from_addr or '(unknown)'}\n"
        f"Subject: Re: {p.email.subject or '(no subject)'}\n"
        f"X-Draft-Provenance: {p.provenance}\n"
        f"X-Triage-Category: {p.result.category}\n"
        f"\n"
    )
    path.write_text(header + draft.rstrip() + "\n", encoding="utf-8")
    return path


def reply_subject(subject: str) -> str:
    """`Re:`-prefix a subject without doubling an existing one."""
    subject = (subject or "").strip()
    if not subject:
        return "Re: (no subject)"
    return subject if subject[:3].lower() == "re:" else f"Re: {subject}"


def build_reply_message(p: ProcessedEmail, draft: str) -> EmailMessage:
    """Build the approved reply as an RFC-5322 message: headers, threading, body.

    Shared by the ``.eml`` export and the IMAP Drafts APPEND. ``To`` is the
    original sender; ``From`` is filled from IMAP_USER when set, otherwise the
    mail client supplies it on open. Threading headers are added only when the
    original carried a real Message-ID (not a synthesized content hash), so
    replies thread correctly in the recipient's client.
    """
    msg = EmailMessage()
    orig = p.email
    if orig.from_addr:
        msg["To"] = orig.from_addr
    sender = os.environ.get("IMAP_USER", "").strip()
    if sender:
        msg["From"] = sender
    msg["Subject"] = reply_subject(orig.subject)
    if orig.id and not orig.id.startswith("<sha1:"):
        msg["In-Reply-To"] = orig.id
        existing_refs = (orig.headers.get("References") or "").strip()
        msg["References"] = f"{existing_refs} {orig.id}".strip()
    msg["X-Draft-Provenance"] = p.provenance
    msg["X-Triage-Category"] = p.result.category
    msg.set_content(draft.rstrip() + "\n")
    return msg


def save_approved_eml(p: ProcessedEmail, draft: str, out_dir: Path) -> Path:
    """Write the approved reply as a ``.eml`` that opens pre-filled in a mail client."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(p.email)
    path = out_dir / f"{stem}.eml"
    n = 2
    while path.exists():
        path = out_dir / f"{stem}-{n}.eml"
        n += 1
    path.write_bytes(build_reply_message(p, draft).as_bytes())
    return path


def source_is_imap(source: str) -> bool:
    """True when an email's source string names an IMAP connection.

    IMAP sources are ``"imap"`` (both ``start-imap`` and ``process --source
    imap``); mbox sources are ``"mbox:<file>"`` (``start``) or ``"mbox"``
    (``process``). An empty/unknown source is treated as mbox.
    """
    return source.strip().lower().startswith("imap")


def persist_approved(
    p: ProcessedEmail,
    draft: str,
    out_dir: Path,
    source: str,
    imap_account: review_queue.ImapAccountRef | None = None,
) -> PersistOutcome:
    """Persist an approved draft, routing by where the email came from.

    Always writes the plain ``.txt`` (the session-log anchor). Then, so the
    reply lands somewhere you can actually send it from:

    - **IMAP source** -> APPEND the reply into your Drafts folder, so it shows
      up ready to send in the same mail client the message came from.
    - **mbox source** -> write a double-clickable ``.eml`` that opens
      pre-filled in your default mail client (there is no live mailbox to
      write a draft into).

    The routed step is best-effort: a failure is reported in the outcome but
    never blocks the review, and nothing here ever sends the mail.
    """
    if source_is_imap(source) and imap_account is not None:
        current_host = os.environ.get("IMAP_HOST", "").strip()
        current_user = os.environ.get("IMAP_USER", "").strip()
        if (current_host, current_user) != (imap_account.host, imap_account.user):
            raise ImapAccountMismatchError(
                "this email was fetched from a different IMAP account; "
                "reconnect that account before approving its draft"
            )

    txt_path = save_approved_draft(p, draft, out_dir)
    note: str | None = None
    warning: str | None = None
    if source_is_imap(source):
        try:
            raw = build_reply_message(p, draft).as_bytes()
            if imap_account is not None:
                append_to_drafts(raw, folder=imap_account.drafts_folder)
            else:
                append_to_drafts(raw)
            note = "saved to IMAP Drafts (not sent)"
        except Exception as exc:
            warning = f"could not save to IMAP Drafts: {exc}"
    else:
        try:
            eml_path = save_approved_eml(p, draft, out_dir)
            note = f".eml written to {eml_path}"
        except Exception as exc:
            warning = f"could not write .eml: {exc}"
    return PersistOutcome(txt_path=txt_path, note=note, warning=warning)


def session_record(
    p: ProcessedEmail, action: str, saved_path: Path | None
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "email_id": p.email.id,
        "from": p.email.from_addr,
        "subject": p.email.subject,
        "category": p.result.category,
        "confidence": p.result.confidence,
        "escalate": p.decision.escalate,
        "score": p.decision.score,
        "reason": p.decision.reason,
        "provenance": p.provenance,
        "claude_used": p.claude_used,
        "num_placeholders": len(p.mapping),
        "action": action,
        "approved_path": str(saved_path) if saved_path else None,
        "error": p.error,
    }


def append_session_record(session_path: Path, record: dict[str, Any]) -> None:
    session_path.parent.mkdir(parents=True, exist_ok=True)
    with session_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
