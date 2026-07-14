"""End-to-end CLI for the email triage pipeline.

Usage:
    python -m src.cli triage-emails    data/dev_corpus.mbox --limit 10
    python -m src.cli anonymize-emails data/dev_corpus.mbox --limit 5
    python -m src.cli process          data/dev_corpus.mbox --limit 10
    python -m src.cli process-old      data/dev_corpus.mbox --limit 10
    python -m src.cli start            data/inbox
    python -m src.cli start-imap       --days 7
    python -m src.cli review
    python -m src.cli reset

``triage-emails`` runs the local model and prints a result panel per email.
``anonymize-emails`` previews exactly what would leave the box on escalation.
``process`` is the full loop: triage locally → score sensitivity → for the
escalated emails, anonymize, delegate to Claude, and rehydrate the reply →
present everything for human approve/reject/edit. Processing runs on a
background thread, so you review the first email while the rest are still
being triaged and delegated; ``process-old`` is the original fully sequential
version (process one, review one, repeat). Both accept ``--source imap`` to
read unread mail over a read-only IMAP connection instead of an mbox file.

``start`` / ``start-imap`` / ``review`` split the pipeline in two: ``start``
processes every new email from a folder of mbox files (``start-imap``: from
the IMAP account) into a persistent queue under ``data/queue/``, ranks the
batch by importance with one anonymized Claude call, and prints a summary
table sorted most-important-first; ``review`` then walks the unreviewed
queue interactively. The eventual single entry point will ask first-run
whether to use local mbox files or IMAP — for now they are separate commands.

Nothing is ever sent automatically; approved drafts are written to
``data/approved_drafts/`` and every decision is appended to
``logs/sessions/<timestamp>.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from itertools import islice
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from src import review_queue
from src.anonymize.ner_anonymizer import CombinedAnonymizer
from src.anonymize.regex_anonymizer import RegexAnonymizer
from src.anonymize.rehydrate import rehydrate
from src.delegate.claude_client import ClaudeClient
from src.ingestion.imap_loader import append_to_drafts, load_imap_unread
from src.ingestion.mbox_loader import Email, load_mbox
from src.router.importance import EmailDigest, rank_importance
from src.router.sensitivity_scorer import EscalationDecision, SensitivityScorer
from src.triage.classifier import TriageResult, triage
from src.triage.ollama_client import OllamaClient

console = Console()


CATEGORY_STYLE: dict[str, str] = {
    "action_required": "yellow",
    "needs_reply": "cyan",
    "fyi": "blue",
    "spam": "red",
    "unclear": "magenta",
}

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _confidence_bar(confidence: float, width: int = 20) -> Text:
    """Build a colored block bar for a confidence in [0, 1]."""
    filled = max(0, min(width, int(round(confidence * width))))
    if confidence >= 0.85:
        color = "green"
    elif confidence >= 0.6:
        color = "yellow"
    else:
        color = "red"
    bar = Text()
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f"  {confidence:.2f}")
    return bar


def _render_email_panel(
    email: Email, result: TriageResult, decision: EscalationDecision
) -> Panel:
    color = CATEGORY_STYLE.get(result.category, "white")

    body = Text()

    def _label(name: str) -> None:
        body.append(f"{name:<11}: ", style="dim")

    _label("from")
    body.append(f"{email.from_addr or '(unknown)'}\n")

    _label("category")
    body.append(result.category, style=f"bold {color}")
    body.append("\n")

    _label("confidence")
    body.append_text(_confidence_bar(result.confidence))
    body.append("\n")

    _label("summary")
    body.append(f"{result.summary}\n")

    if result.extracted_action_items:
        body.append("\n")
        body.append("action items:\n", style="dim")
        for item in result.extracted_action_items:
            body.append("  • ", style="dim")
            body.append(f"{item}\n")

    if result.suggested_reply_draft:
        body.append("\n")
        body.append("draft reply:\n", style="dim")
        body.append(result.suggested_reply_draft, style="italic")
        body.append("\n")

    body.append("\n")
    _label("escalate")
    if decision.escalate:
        body.append("true", style="bold red")
        body.append(f"  (score {decision.score:.2f} — run `process` to delegate)", style="dim")
    else:
        body.append("false", style="dim")
        body.append(f"  (score {decision.score:.2f})", style="dim")
    body.append("\n")
    _label("reason")
    body.append(decision.reason, style="dim")

    title = email.subject.strip() if email.subject else "(no subject)"
    return Panel(body, title=title, title_align="left", border_style=color)


def _render_failure_panel(email: Email, exc: Exception) -> Panel:
    body = Text()
    body.append("from       : ", style="dim")
    body.append(f"{email.from_addr}\n")
    body.append("error      : ", style="dim")
    body.append(f"{type(exc).__name__}: {exc}", style="red")
    title = email.subject.strip() if email.subject else "(no subject)"
    return Panel(body, title=title, title_align="left", border_style="red")


# ---------------------------------------------------------------------------
# Subcommand: triage-emails
# ---------------------------------------------------------------------------


def _cmd_triage_emails(args: argparse.Namespace) -> int:
    mbox_path = Path(args.mbox_path)
    if not mbox_path.exists():
        console.print(f"[red]mbox not found:[/] {mbox_path}")
        return 1

    client = OllamaClient()
    try:
        scorer = SensitivityScorer(config_path=args.config)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]router config error:[/] {exc}")
        return 1
    console.print(
        f"[dim]Reading[/] [bold]{mbox_path}[/] "
        f"[dim](limit {args.limit}, model {client.model})[/]"
    )
    console.print()

    processed = 0
    failed = 0
    escalated = 0

    if args.shuffle:
        all_emails = list(load_mbox(mbox_path))
        rng = random.Random(args.seed)
        if args.limit < len(all_emails):
            emails_iter: list[Email] = rng.sample(all_emails, args.limit)
        else:
            emails_iter = all_emails
            rng.shuffle(emails_iter)
        seed_note = f", seed {args.seed}" if args.seed is not None else ""
        console.print(
            f"[dim](shuffled {len(emails_iter)} of {len(all_emails)} emails{seed_note})[/]"
        )
        emails: object = emails_iter
    else:
        emails = islice(load_mbox(mbox_path), args.limit)

    for i, email in enumerate(emails, start=1):
        subject_preview = (email.subject or "(no subject)")[:60]
        try:
            with console.status(
                f"[cyan]Triaging [{i}] {subject_preview}",
                spinner="dots",
            ):
                result = triage(email, client=client)
        except Exception as exc:
            console.print(_render_failure_panel(email, exc))
            failed += 1
            processed += 1
            continue

        decision = scorer.score(email, result)
        if decision.escalate:
            escalated += 1

        console.print(_render_email_panel(email, result, decision))
        processed += 1

    console.rule(style="dim")
    console.print(
        f"Processed [bold]{processed}[/]  •  "
        f"[red]{failed}[/] failed  •  "
        f"[yellow]{escalated}[/] flagged for escalation"
    )
    return 0 if failed == 0 else 2


# ---------------------------------------------------------------------------
# Subcommand: anonymize-emails  (preview of the Claude delegation payload)
# ---------------------------------------------------------------------------


# Preview-only constants. The real delegate (prompt 14) will own these for
# production use; here they exist so the user can eyeball exactly what would
# leave the box on an escalation.
CLAUDE_PREVIEW_MODEL = "claude-opus-4-7"
CLAUDE_PREVIEW_MAX_TOKENS = 1024
CLAUDE_SYSTEM_PROMPT = (
    "You are an email triage assistant. The user message contains an "
    "anonymized email. Names, organizations, phone numbers, dollar amounts, "
    "dates, and other PII have been replaced with proper-noun-shaped "
    "placeholders such as Alex_P1, Acme_O1, Phone_F1, Date_D1. Preserve "
    "every placeholder verbatim in your response — do not invent new "
    "placeholders, do not paraphrase them, do not strip their suffixes."
)
DEFAULT_PREVIEW_TASK = (
    "Draft a concise reply to the email below. Reply in plain text only."
)

_PLACEHOLDER_RE = re.compile(r"[A-Z][a-z]+_[A-Z]\d+")


def _build_anonymizer(name: str) -> tuple[Any, str]:
    if name == "regex":
        return RegexAnonymizer(), "regex"
    if name == "combined":
        return CombinedAnonymizer(), "regex+ner"
    if name == "coref":
        from src.anonymize.coref_anonymizer import CorefAnonymizer
        return CorefAnonymizer(), "regex+ner+coref"
    raise ValueError(f"unknown anonymizer: {name!r}")


def _build_user_message(email: Email, task: str) -> str:
    """Render the full Claude user message in plain text.

    Subject and From are included alongside the body so the anonymizer can
    redact PII from all three in a single pass — keeping placeholder
    numbering consistent across header and body.
    """
    return (
        f"Task: {task}\n\n"
        f"Subject: {email.subject or '(no subject)'}\n"
        f"From: {email.from_addr or '(unknown)'}\n"
        f"\n"
        f"{email.body_plain}"
    )


def _build_claude_request(
    anonymized_user_message: str,
) -> dict[str, Any]:
    return {
        "model": CLAUDE_PREVIEW_MODEL,
        "max_tokens": CLAUDE_PREVIEW_MAX_TOKENS,
        "system": CLAUDE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": anonymized_user_message}],
    }


def _highlight_placeholders(text: str) -> Text:
    out = Text()
    cursor = 0
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.start() > cursor:
            out.append(text[cursor : m.start()])
        out.append(m.group(0), style="bold cyan")
        cursor = m.end()
    if cursor < len(text):
        out.append(text[cursor:])
    return out


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n[… {len(text) - max_chars} more chars truncated]"


def _render_anonymize_panel(
    email: Email,
    original_user_message: str,
    anonymized_user_message: str,
    mapping: dict[str, str],
    anonymizer_label: str,
    claude_request: dict[str, Any],
    *,
    max_body_chars: int,
) -> Panel:
    border = "cyan" if mapping else "dim"
    body = Text()

    def _label(name: str) -> None:
        body.append(f"{name:<13}: ", style="dim")

    _label("from")
    body.append(f"{email.from_addr or '(unknown)'}\n")

    _label("anonymizer")
    body.append(anonymizer_label)
    body.append(
        f"  •  {len(mapping)} placeholder{'' if len(mapping) == 1 else 's'}",
        style="dim",
    )
    body.append("\n\n")

    body.append("original message:\n", style="dim")
    body.append(_truncate(original_user_message, max_body_chars))
    body.append("\n\n")

    body.append("anonymized message (what Claude would see):\n", style="dim")
    body.append_text(_highlight_placeholders(_truncate(anonymized_user_message, max_body_chars)))
    body.append("\n\n")

    body.append("mapping:\n", style="dim")
    if mapping:
        for placeholder, value in mapping.items():
            body.append("  • ", style="dim")
            body.append(placeholder, style="bold cyan")
            body.append(" → ", style="dim")
            body.append(f"{value}\n")
    else:
        body.append("  (none — nothing in this email was anonymized)\n", style="dim")
    body.append("\n")

    body.append("Claude API request (preview — not sent):\n", style="dim")
    body.append(json.dumps(claude_request, indent=2, ensure_ascii=False))

    title = email.subject.strip() if email.subject else "(no subject)"
    return Panel(body, title=title, title_align="left", border_style=border)


def _cmd_anonymize_emails(args: argparse.Namespace) -> int:
    mbox_path = Path(args.mbox_path)
    if not mbox_path.exists():
        console.print(f"[red]mbox not found:[/] {mbox_path}")
        return 1

    with console.status(
        f"[cyan]Loading anonymizer ({args.anonymizer})…", spinner="dots"
    ):
        anonymizer, anon_label = _build_anonymizer(args.anonymizer)

    console.print(
        f"[dim]Reading[/] [bold]{mbox_path}[/] "
        f"[dim](limit {args.limit}, anonymizer {anon_label}, "
        f"model {CLAUDE_PREVIEW_MODEL})[/]"
    )
    console.print()

    if args.shuffle:
        all_emails = list(load_mbox(mbox_path))
        rng = random.Random(args.seed)
        if args.limit < len(all_emails):
            emails_iter: list[Email] = rng.sample(all_emails, args.limit)
        else:
            emails_iter = all_emails
            rng.shuffle(emails_iter)
        seed_note = f", seed {args.seed}" if args.seed is not None else ""
        console.print(
            f"[dim](shuffled {len(emails_iter)} of {len(all_emails)} emails{seed_note})[/]"
        )
        emails: object = emails_iter
    else:
        emails = islice(load_mbox(mbox_path), args.limit)

    processed = 0
    failed = 0
    total_placeholders = 0

    for i, email in enumerate(emails, start=1):
        subject_preview = (email.subject or "(no subject)")[:60]
        try:
            user_message = _build_user_message(email, args.task)
            with console.status(
                f"[cyan]Anonymizing [{i}] {subject_preview}", spinner="dots"
            ):
                anonymized, mapping = anonymizer.anonymize(user_message)
        except Exception as exc:
            console.print(_render_failure_panel(email, exc))
            failed += 1
            processed += 1
            continue

        claude_request = _build_claude_request(anonymized)
        console.print(
            _render_anonymize_panel(
                email,
                user_message,
                anonymized,
                mapping,
                anon_label,
                claude_request,
                max_body_chars=args.max_chars,
            )
        )
        total_placeholders += len(mapping)
        processed += 1

    console.rule(style="dim")
    console.print(
        f"Processed [bold]{processed}[/]  •  "
        f"[red]{failed}[/] failed  •  "
        f"[cyan]{total_placeholders}[/] total placeholders"
    )
    return 0 if failed == 0 else 2


# ---------------------------------------------------------------------------
# Subcommands: process / process-old  (full pipeline — triage, escalate,
# delegate, review)
# ---------------------------------------------------------------------------


DEFAULT_PROCESS_TASK = (
    "Draft a concise, professional reply to the email below. Reply in plain "
    "text only, and preserve every placeholder token (e.g. Alex_P1) verbatim."
)

PROVENANCE_STYLE = {"local": "blue", "Claude": "magenta"}

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ProcessedEmail:
    """One email after triage + (optional) escalation, ready to present."""

    email: Email
    result: TriageResult
    decision: EscalationDecision
    draft: str | None
    provenance: str            # "local" or "Claude"
    mapping: dict[str, str]    # placeholder -> original (empty unless escalated)
    claude_used: bool
    error: str | None          # escalation-path error note, if any


def _email_payload(email: Email) -> str:
    """The subject/from/body block that gets anonymized and sent to Claude.

    The task instruction is added by the Claude client, not here, so it isn't
    run through the anonymizer (it carries no PII).
    """
    return (
        f"Subject: {email.subject or '(no subject)'}\n"
        f"From: {email.from_addr or '(unknown)'}\n"
        f"\n"
        f"{email.body_plain or ''}"
    )


def _build_processed(
    email: Email,
    result: TriageResult,
    decision: EscalationDecision,
    *,
    anonymizer: Any,
    claude_client: ClaudeClient | None,
    task: str,
) -> ProcessedEmail:
    """Produce the draft for one email, escalating to Claude when decided.

    On escalation the email is anonymized, sent to Claude, and the reply is
    rehydrated locally. Any failure in that path is captured (not raised) and
    we fall back to the local draft so the reviewer still sees something.
    """
    draft = result.suggested_reply_draft
    provenance = "local"
    mapping: dict[str, str] = {}
    claude_used = False
    error: str | None = None

    if decision.escalate:
        if claude_client is None:
            error = "escalated, but no Claude client available — kept local draft"
        else:
            try:
                anonymized, mapping = anonymizer.anonymize(_email_payload(email))
                claude_reply = claude_client.delegate(anonymized, None, task)
                draft = rehydrate(claude_reply, mapping)
                provenance = "Claude"
                claude_used = True
            except Exception as exc:  # network / API / anonymizer — degrade gracefully
                error = (
                    f"delegation failed ({type(exc).__name__}: {exc}); "
                    f"kept local draft"
                )

    return ProcessedEmail(
        email=email,
        result=result,
        decision=decision,
        draft=draft,
        provenance=provenance,
        mapping=mapping,
        claude_used=claude_used,
        error=error,
    )


def _render_process_panel(p: ProcessedEmail, *, max_body_chars: int) -> Panel:
    result = p.result
    color = CATEGORY_STYLE.get(result.category, "white")
    body = Text()

    def _label(name: str) -> None:
        body.append(f"{name:<12}: ", style="dim")

    # --- original email ---
    _label("from")
    body.append(f"{p.email.from_addr or '(unknown)'}\n")
    body.append("original message:\n", style="dim")
    body.append(_truncate(p.email.body_plain or "(empty body)", max_body_chars))
    body.append("\n\n")

    # --- local classification ---
    _label("category")
    body.append(result.category, style=f"bold {color}")
    body.append("\n")
    _label("confidence")
    body.append_text(_confidence_bar(result.confidence))
    body.append("\n")
    _label("summary")
    body.append(f"{result.summary}\n")
    if result.extracted_action_items:
        body.append("action items:\n", style="dim")
        for item in result.extracted_action_items:
            body.append("  • ", style="dim")
            body.append(f"{item}\n")
    body.append("\n")

    # --- escalation decision ---
    _label("escalate")
    if p.decision.escalate:
        body.append("true", style="bold red")
    else:
        body.append("false", style="green")
    body.append(f"  (score {p.decision.score:.2f})\n", style="dim")
    _label("reason")
    body.append(f"{p.decision.reason}\n", style="dim")
    if p.claude_used:
        _label("delegation")
        body.append(
            f"anonymized → Claude → rehydrated "
            f"({len(p.mapping)} placeholder{'' if len(p.mapping) == 1 else 's'})\n",
            style="dim",
        )
    if p.error:
        _label("note")
        body.append(f"{p.error}\n", style="yellow")
    body.append("\n")

    # --- draft + provenance ---
    prov_style = PROVENANCE_STYLE.get(p.provenance, "white")
    _label("draft")
    body.append(f"[{p.provenance}]\n", style=f"bold {prov_style}")
    if p.draft and p.draft.strip():
        body.append(p.draft, style="italic")
    else:
        body.append("(no draft — local model didn't propose one)", style="dim")
    body.append("\n")

    title = p.email.subject.strip() if p.email.subject else "(no subject)"
    return Panel(body, title=title, title_align="left", border_style=color)


def _prompt_action(has_draft: bool) -> str:
    """Ask the reviewer what to do. Returns approve/edit/reject/quit."""
    if not has_draft:
        choice = Prompt.ask(
            "  [bold]action[/] — \\[r]eject/skip, \\[q]uit",
            choices=["r", "q"],
            default="r",
        )
        return {"r": "reject", "q": "quit"}[choice]
    choice = Prompt.ask(
        "  [bold]action[/] — \\[a]pprove, \\[e]dit, \\[r]eject, \\[q]uit",
        choices=["a", "e", "r", "q"],
        default="a",
    )
    return {"a": "approve", "e": "edit", "r": "reject", "q": "quit"}[choice]


def _edit_draft(draft: str) -> str:
    """Open the draft in $EDITOR (fallback: a one-line prompt) and return it."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        console.print("[dim]No $EDITOR set — type a replacement draft below.[/]")
        return Prompt.ask("  edited draft", default=draft)
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as tf:
        tf.write(draft)
        tmp_path = Path(tf.name)
    try:
        subprocess.run([*editor.split(), str(tmp_path)], check=True)
        return tmp_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        console.print(f"[yellow]editor failed ({exc}); keeping original draft[/]")
        return draft
    finally:
        tmp_path.unlink(missing_ok=True)


def _safe_stem(email: Email) -> str:
    raw = (email.id or "email").strip().strip("<>")
    stem = _UNSAFE_FILENAME_RE.sub("_", raw).strip("_")
    return (stem or "email")[:80]


def _save_approved_draft(p: ProcessedEmail, draft: str, out_dir: Path) -> Path:
    """Write an approved draft to ``out_dir``; never overwrites an existing file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(p.email)
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


def _reply_subject(subject: str) -> str:
    """`Re:`-prefix a subject without doubling an existing one."""
    subject = (subject or "").strip()
    if not subject:
        return "Re: (no subject)"
    return subject if subject[:3].lower() == "re:" else f"Re: {subject}"


def _build_reply_message(p: ProcessedEmail, draft: str) -> EmailMessage:
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
    msg["Subject"] = _reply_subject(orig.subject)
    if orig.id and not orig.id.startswith("<sha1:"):
        msg["In-Reply-To"] = orig.id
        existing_refs = (orig.headers.get("References") or "").strip()
        msg["References"] = f"{existing_refs} {orig.id}".strip()
    msg["X-Draft-Provenance"] = p.provenance
    msg["X-Triage-Category"] = p.result.category
    msg.set_content(draft.rstrip() + "\n")
    return msg


def _save_approved_eml(p: ProcessedEmail, draft: str, out_dir: Path) -> Path:
    """Write the approved reply as a ``.eml`` that opens pre-filled in a mail client."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(p.email)
    path = out_dir / f"{stem}.eml"
    n = 2
    while path.exists():
        path = out_dir / f"{stem}-{n}.eml"
        n += 1
    path.write_bytes(_build_reply_message(p, draft).as_bytes())
    return path


def _source_is_imap(source: str) -> bool:
    """True when an email's source string names an IMAP connection.

    IMAP sources are ``"imap"`` (both ``start-imap`` and ``process --source
    imap``); mbox sources are ``"mbox:<file>"`` (``start``) or ``"mbox"``
    (``process``). An empty/unknown source is treated as mbox.
    """
    return source.strip().lower().startswith("imap")


def _persist_approved(
    p: ProcessedEmail, draft: str, out_dir: Path, source: str
) -> Path:
    """Persist an approved draft, routing by where the email came from.

    Always writes the plain ``.txt`` (the session-log anchor). Then, so the
    reply lands somewhere you can actually send it from:

    - **IMAP source** -> APPEND the reply into your Drafts folder, so it shows
      up ready to send in the same mail client the message came from.
    - **mbox source** -> write a double-clickable ``.eml`` that opens
      pre-filled in your default mail client (there is no live mailbox to
      write a draft into).

    The routed step is best-effort: a failure is reported but never blocks the
    review, and nothing here ever sends the mail. Returns the ``.txt`` path.
    """
    txt_path = _save_approved_draft(p, draft, out_dir)
    if _source_is_imap(source):
        try:
            append_to_drafts(_build_reply_message(p, draft).as_bytes())
            console.print("  [dim]saved to IMAP Drafts (not sent)[/]")
        except Exception as exc:
            console.print(f"  [yellow]could not save to IMAP Drafts: {exc}[/]")
    else:
        try:
            eml_path = _save_approved_eml(p, draft, out_dir)
            console.print(f"  [dim].eml written to {eml_path}[/]")
        except Exception as exc:
            console.print(f"  [yellow]could not write .eml: {exc}[/]")
    return txt_path


def _session_record(
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


def _append_session_record(session_path: Path, record: dict[str, Any]) -> None:
    session_path.parent.mkdir(parents=True, exist_ok=True)
    with session_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _limit_and_shuffle(
    all_emails: list[Email], limit: int, shuffle: bool, seed: int | None
) -> list[Email]:
    """First N of a list, or an (optionally seeded) random N."""
    if not shuffle:
        return all_emails[:limit]
    rng = random.Random(seed)
    if limit < len(all_emails):
        chosen = rng.sample(all_emails, limit)
    else:
        chosen = list(all_emails)
        rng.shuffle(chosen)
    seed_note = f", seed {seed}" if seed is not None else ""
    console.print(
        f"[dim](shuffled {len(chosen)} of {len(all_emails)} emails{seed_note})[/]"
    )
    return chosen


def _select_emails(
    mbox_path: Path, limit: int, shuffle: bool, seed: int | None
) -> list[Email]:
    """Pick emails from the mbox: first N, or a (optionally seeded) random N."""
    if shuffle:
        return _limit_and_shuffle(list(load_mbox(mbox_path)), limit, shuffle, seed)
    return list(islice(load_mbox(mbox_path), limit))


def _validate_source_args(args: argparse.Namespace) -> bool:
    """Check the mbox/imap source flags; print the problem and return False."""
    if args.source == "imap":
        return True
    if not args.mbox_path:
        console.print("[red]mbox_path is required with --source mbox[/]")
        return False
    if not Path(args.mbox_path).exists():
        console.print(f"[red]mbox not found:[/] {args.mbox_path}")
        return False
    return True


def _source_label(args: argparse.Namespace) -> str:
    if args.source == "imap":
        return f"imap (unread, last {args.days} days)"
    return str(args.mbox_path)


def _collect_process_emails(args: argparse.Namespace) -> list[Email] | None:
    """Load the emails for `process`/`process-old` from mbox or IMAP.

    Returns None after printing an error so the caller can exit 1.
    """
    if args.source == "imap":
        try:
            with console.status(
                f"[cyan]Fetching unread (last {args.days} days) via IMAP…",
                spinner="dots",
            ):
                all_emails = load_imap_unread(days=args.days)
        except Exception as exc:
            console.print(f"[red]IMAP error:[/] {exc}")
            return None
        console.print(f"[dim]IMAP returned {len(all_emails)} unread email(s)[/]")
        return _limit_and_shuffle(all_emails, args.limit, args.shuffle, args.seed)
    return _select_emails(Path(args.mbox_path), args.limit, args.shuffle, args.seed)


def _cmd_process_old(args: argparse.Namespace) -> int:
    """Sequential pipeline: process one email, review it, then move on."""
    if not _validate_source_args(args):
        return 1

    try:
        scorer = SensitivityScorer(config_path=args.config)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]router config error:[/] {exc}")
        return 1

    ollama_client = OllamaClient()
    with console.status(
        f"[cyan]Loading anonymizer ({args.anonymizer})…", spinner="dots"
    ):
        anonymizer, anon_label = _build_anonymizer(args.anonymizer)

    out_dir = Path(args.approved_dir)
    session_path = (
        Path(args.sessions_dir) / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    interactive = sys.stdin.isatty() and not args.no_input
    claude_client: ClaudeClient | None = None

    console.print(
        f"[dim]Processing[/] [bold]{_source_label(args)}[/] "
        f"[dim](limit {args.limit}, anonymizer {anon_label}, "
        f"triage {ollama_client.model})[/]"
    )
    if not interactive:
        console.print("[dim](non-interactive: presenting + logging only, no prompts)[/]")
    console.print(f"[dim]session log → {session_path}[/]")
    console.print()

    emails = _collect_process_emails(args)
    if emails is None:
        return 1

    processed = failed = escalated = approved = 0
    quit_early = False

    for i, email in enumerate(emails, start=1):
        subject_preview = (email.subject or "(no subject)")[:60]
        try:
            with console.status(
                f"[cyan]Triaging [{i}] {subject_preview}", spinner="dots"
            ):
                result = triage(email, client=ollama_client)
        except Exception as exc:
            console.print(_render_failure_panel(email, exc))
            _append_session_record(
                session_path,
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "email_id": email.id,
                    "from": email.from_addr,
                    "subject": email.subject,
                    "action": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            failed += 1
            processed += 1
            continue

        decision = scorer.score(email, result)
        if decision.escalate:
            escalated += 1
            # Construct the Claude client lazily, on the first escalation, so a
            # run with nothing to escalate never needs an API key.
            if claude_client is None:
                try:
                    claude_client = ClaudeClient()
                except Exception as exc:
                    console.print(
                        f"[yellow]Claude client unavailable ({exc}); "
                        f"escalations will keep their local draft[/]"
                    )

        verb = "Delegating" if decision.escalate else "Drafting"
        with console.status(
            f"[cyan]{verb} [{i}] {subject_preview}", spinner="dots"
        ):
            p = _build_processed(
                email,
                result,
                decision,
                anonymizer=anonymizer,
                claude_client=claude_client,
                task=args.task,
            )

        console.print(_render_process_panel(p, max_body_chars=args.max_chars))

        action = "presented"
        saved_path: Path | None = None
        if interactive:
            action = _prompt_action(bool(p.draft and p.draft.strip()))
            if action == "quit":
                _append_session_record(session_path, _session_record(p, "quit", None))
                quit_early = True
                processed += 1
                break
            draft_to_save = p.draft or ""
            if action == "edit":
                draft_to_save = _edit_draft(draft_to_save)
            if action in ("approve", "edit") and draft_to_save.strip():
                saved_path = _persist_approved(p, draft_to_save, out_dir, args.source)
                approved += 1
                console.print(f"  [green]saved →[/] {saved_path}")
            elif action == "reject":
                console.print("  [dim]rejected — nothing saved[/]")

        _append_session_record(session_path, _session_record(p, action, saved_path))
        processed += 1
        console.print()

    console.rule(style="dim")
    note = " (stopped early)" if quit_early else ""
    console.print(
        f"Processed [bold]{processed}[/]{note}  •  "
        f"[yellow]{escalated}[/] escalated  •  "
        f"[green]{approved}[/] approved  •  "
        f"[red]{failed}[/] failed"
    )
    console.print(f"[dim]Decisions logged to[/] {session_path}")
    return 0 if failed == 0 else 2


@dataclass
class _ProcessOutcome:
    """One email's background-processing result, handed to the review loop.

    ``processed`` is None when triage itself failed; ``error`` then carries
    the exception. ``notes`` are one-time warnings raised on the worker (e.g.
    Claude client construction failed) for the review loop to print — the
    worker never touches the console itself.
    """

    email: Email
    processed: ProcessedEmail | None
    error: Exception | None
    notes: list[str]


class _WorkerLogCapture(logging.Handler):
    """Buffers project log output emitted while the worker runs.

    ``rehydrate()`` (and any other ``src.*`` module) logs warnings straight to
    stderr by default; from the worker thread that text would land in the
    middle of whatever the reviewer is typing at the prompt. Buffer it here
    and ship it through the outcome queue as notes instead.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def drain(self) -> list[str]:
        out, self.messages = self.messages, []
        return out


def _process_worker(
    emails: list[Email],
    outcomes: queue.Queue[_ProcessOutcome | None],
    stop: threading.Event,
    *,
    ollama_client: OllamaClient,
    scorer: SensitivityScorer,
    anonymizer: Any,
    task: str,
) -> None:
    """Triage / score / delegate every email, queueing each result as it lands.

    Runs on a background thread so the reviewer can act on early emails while
    later ones are still processing. Emails are handled in order, one at a
    time (the local model is the bottleneck; parallel calls wouldn't help).
    Ends the stream with a ``None`` sentinel; checks ``stop`` between emails
    so a quit doesn't keep burning model calls.
    """
    capture = _WorkerLogCapture()
    src_logger = logging.getLogger("src")
    src_logger.addHandler(capture)
    prev_propagate = src_logger.propagate
    src_logger.propagate = False  # keep records off stderr while we hold them

    claude_client: ClaudeClient | None = None
    claude_init_failed = False
    try:
        for email in emails:
            if stop.is_set():
                break
            notes: list[str] = []
            try:
                result = triage(email, client=ollama_client)
            except Exception as exc:
                notes.extend(capture.drain())
                outcomes.put(
                    _ProcessOutcome(email=email, processed=None, error=exc, notes=notes)
                )
                continue

            decision = scorer.score(email, result)
            # Construct the Claude client lazily, on the first escalation, so a
            # run with nothing to escalate never needs an API key.
            if decision.escalate and claude_client is None and not claude_init_failed:
                try:
                    claude_client = ClaudeClient()
                except Exception as exc:
                    claude_init_failed = True
                    notes.append(
                        f"Claude client unavailable ({exc}); "
                        f"escalations will keep their local draft"
                    )

            p = _build_processed(
                email,
                result,
                decision,
                anonymizer=anonymizer,
                claude_client=claude_client,
                task=task,
            )
            notes.extend(capture.drain())
            outcomes.put(
                _ProcessOutcome(email=email, processed=p, error=None, notes=notes)
            )
    finally:
        src_logger.removeHandler(capture)
        src_logger.propagate = prev_propagate
        # Sentinel goes in the finally so an unexpected worker crash can't
        # leave the review loop blocked on the queue forever.
        outcomes.put(None)


def _cmd_process(args: argparse.Namespace) -> int:
    """Pipelined version: a worker thread processes every email up front
    while the foreground loop presents each one for review as soon as it is
    ready — reviewing email 1 never waits for emails 2..N to finish."""
    if not _validate_source_args(args):
        return 1

    try:
        scorer = SensitivityScorer(config_path=args.config)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]router config error:[/] {exc}")
        return 1

    ollama_client = OllamaClient()
    with console.status(
        f"[cyan]Loading anonymizer ({args.anonymizer})…", spinner="dots"
    ):
        anonymizer, anon_label = _build_anonymizer(args.anonymizer)

    out_dir = Path(args.approved_dir)
    session_path = (
        Path(args.sessions_dir) / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    interactive = sys.stdin.isatty() and not args.no_input

    console.print(
        f"[dim]Processing[/] [bold]{_source_label(args)}[/] "
        f"[dim](limit {args.limit}, anonymizer {anon_label}, "
        f"triage {ollama_client.model}, background processing)[/]"
    )
    if not interactive:
        console.print("[dim](non-interactive: presenting + logging only, no prompts)[/]")
    console.print(f"[dim]session log → {session_path}[/]")
    console.print()

    emails = _collect_process_emails(args)
    if emails is None:
        return 1

    outcomes: queue.Queue[_ProcessOutcome | None] = queue.Queue()
    stop = threading.Event()
    worker = threading.Thread(
        target=_process_worker,
        args=(emails, outcomes, stop),
        kwargs={
            "ollama_client": ollama_client,
            "scorer": scorer,
            "anonymizer": anonymizer,
            "task": args.task,
        },
        daemon=True,  # quitting mid-review must not wait out an in-flight call
    )
    worker.start()

    processed = failed = escalated = approved = 0
    quit_early = False

    # The worker preserves order, so outcome i pairs with emails[i].
    for i, email in enumerate(emails, start=1):
        subject_preview = (email.subject or "(no subject)")[:60]
        with console.status(
            f"[cyan]Processing [{i}] {subject_preview}", spinner="dots"
        ):
            item = outcomes.get()
        if item is None:  # defensive: stream ended early
            break

        for note in item.notes:
            console.print(Text(note, style="yellow"))

        if item.processed is None:
            assert item.error is not None
            console.print(_render_failure_panel(item.email, item.error))
            _append_session_record(
                session_path,
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "email_id": item.email.id,
                    "from": item.email.from_addr,
                    "subject": item.email.subject,
                    "action": "error",
                    "error": f"{type(item.error).__name__}: {item.error}",
                },
            )
            failed += 1
            processed += 1
            continue

        p = item.processed
        if p.decision.escalate:
            escalated += 1

        console.print(_render_process_panel(p, max_body_chars=args.max_chars))

        action = "presented"
        saved_path: Path | None = None
        if interactive:
            action = _prompt_action(bool(p.draft and p.draft.strip()))
            if action == "quit":
                stop.set()
                _append_session_record(session_path, _session_record(p, "quit", None))
                quit_early = True
                processed += 1
                break
            draft_to_save = p.draft or ""
            if action == "edit":
                draft_to_save = _edit_draft(draft_to_save)
            if action in ("approve", "edit") and draft_to_save.strip():
                saved_path = _persist_approved(p, draft_to_save, out_dir, args.source)
                approved += 1
                console.print(f"  [green]saved →[/] {saved_path}")
            elif action == "reject":
                console.print("  [dim]rejected — nothing saved[/]")

        _append_session_record(session_path, _session_record(p, action, saved_path))
        processed += 1
        console.print()

    console.rule(style="dim")
    note = " (stopped early)" if quit_early else ""
    console.print(
        f"Processed [bold]{processed}[/]{note}  •  "
        f"[yellow]{escalated}[/] escalated  •  "
        f"[green]{approved}[/] approved  •  "
        f"[red]{failed}[/] failed"
    )
    console.print(f"[dim]Decisions logged to[/] {session_path}")
    return 0 if failed == 0 else 2


# ---------------------------------------------------------------------------
# Subcommands: start / start-imap / review  (queue-based pipeline)
# ---------------------------------------------------------------------------


def _processed_from_record(rec: review_queue.QueueRecord) -> ProcessedEmail:
    """Rebuild the panel-ready ProcessedEmail from a stored queue record."""
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


def _format_importance(importance: float) -> str:
    return f"{importance:.0f}" if float(importance).is_integer() else f"{importance:.1f}"


def _print_queue_summary(pending: list[review_queue.QueueRecord]) -> None:
    """Rich table of everything awaiting review, most important first."""
    if not pending:
        console.print("[green]Review queue is empty — nothing awaiting review.[/]")
        return
    table = Table(
        title=f"Review queue — {len(pending)} email(s) awaiting review",
        title_justify="left",
        show_lines=True,
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("imp", justify="right", width=4)
    table.add_column("email", overflow="fold")

    for i, rec in enumerate(pending, start=1):
        imp = rec.importance
        imp_style = "bold red" if imp >= 8 else "yellow" if imp >= 6 else "dim"

        details = Text()
        details.append(rec.email.subject or "(no subject)", style="bold")
        details.append("\n")
        details.append(f"from {rec.email.from_addr or '(unknown)'}", style="dim")
        details.append("  •  ", style="dim")
        details.append(
            rec.result.category,
            style=CATEGORY_STYLE.get(rec.result.category, "white"),
        )
        details.append("  •  draft: ", style="dim")
        details.append(
            rec.provenance, style=PROVENANCE_STYLE.get(rec.provenance, "white")
        )
        if rec.decision.escalate:
            details.append("  •  escalated", style="red")
        details.append("\n")
        details.append(_truncate(" ".join(rec.result.summary.split()), 220))
        for item in rec.result.extracted_action_items[:3]:
            details.append("\n  • ", style="dim")
            details.append(_truncate(" ".join(item.split()), 110))
        if rec.importance_reason:
            details.append("\n")
            details.append(
                f"({_truncate(rec.importance_reason, 110)})", style="dim italic"
            )

        table.add_row(str(i), Text(_format_importance(imp), style=imp_style), details)
    console.print(table)


def _run_start_pipeline(
    emails: list[Email],
    sources: dict[str, str],
    args: argparse.Namespace,
    queue_dir: Path,
) -> int:
    """Process ``emails`` into the queue with a progress bar, rank, summarize.

    The shared back half of `start` and `start-imap`: background-worker
    processing with a spinner + progress display, one anonymized Claude call
    to rank the batch by importance, append everything to the queue, then
    print the pending-review summary table (newly processed plus anything
    older that was never reviewed).
    """
    failed = 0
    done: list[_ProcessOutcome] = []

    if emails:
        try:
            scorer = SensitivityScorer(config_path=args.config)
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]router config error:[/] {exc}")
            return 1
        ollama_client = OllamaClient()
        with console.status(
            f"[cyan]Loading anonymizer ({args.anonymizer})…", spinner="dots"
        ):
            anonymizer, anon_label = _build_anonymizer(args.anonymizer)
        console.print(
            f"[dim]Processing {len(emails)} email(s) "
            f"(anonymizer {anon_label}, triage {ollama_client.model})[/]"
        )

        outcomes: queue.Queue[_ProcessOutcome | None] = queue.Queue()
        stop = threading.Event()
        worker = threading.Thread(
            target=_process_worker,
            args=(emails, outcomes, stop),
            kwargs={
                "ollama_client": ollama_client,
                "scorer": scorer,
                "anonymizer": anonymizer,
                "task": args.task,
            },
            daemon=True,
        )
        worker.start()

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        with progress:
            task_id = progress.add_task("Processing…", total=len(emails))
            while True:
                item = outcomes.get()
                if item is None:
                    break
                for note in item.notes:
                    progress.console.print(Text(note, style="yellow"))
                subject_preview = (item.email.subject or "(no subject)")[:48]
                if item.processed is None:
                    failed += 1
                    progress.console.print(
                        Text(
                            f"failed: {subject_preview} "
                            f"({type(item.error).__name__}: {item.error})",
                            style="red",
                        )
                    )
                else:
                    done.append(item)
                progress.update(
                    task_id, advance=1, description=f"[cyan]{subject_preview}"
                )
            progress.update(task_id, description="[green]processing done")

        if done:
            digests = [
                EmailDigest(
                    email_id=o.email.id,
                    subject=o.email.subject or "",
                    summary=o.processed.result.summary,
                    action_items=tuple(o.processed.result.extracted_action_items),
                    category=o.processed.result.category,
                    escalate=o.processed.decision.escalate,
                    escalation_score=o.processed.decision.score,
                )
                for o in done
            ]
            claude_client: ClaudeClient | None = None
            try:
                # The ranking reply is ~40 output tokens per email; the default
                # 1024 budget truncates the JSON array on big batches.
                claude_client = ClaudeClient(
                    max_tokens=min(8192, max(1024, 64 * len(digests)))
                )
            except Exception as exc:
                console.print(
                    Text(f"Claude unavailable for ranking ({exc})", style="yellow")
                )
            with console.status(
                "[cyan]Ranking importance (one anonymized Claude call)…",
                spinner="dots",
            ):
                ranking = rank_importance(
                    digests, claude_client=claude_client, anonymizer=anonymizer
                )

            now = datetime.now().astimezone().isoformat()
            new_records = []
            for o in done:
                p = o.processed
                ranked = ranking.scores[o.email.id]
                new_records.append(
                    review_queue.QueueRecord(
                        email=o.email,
                        result=p.result,
                        decision=p.decision,
                        draft=p.draft,
                        provenance=p.provenance,
                        mapping=p.mapping,
                        claude_used=p.claude_used,
                        error=p.error,
                        importance=ranked.importance,
                        importance_reason=ranked.reason,
                        ranked_by=ranking.ranked_by,
                        source=sources.get(o.email.id, ""),
                        processed_at=now,
                    )
                )
            review_queue.append_records(queue_dir, new_records)
            console.print(
                f"[dim]Importance ranked by {ranking.ranked_by}  •  queue → "
                f"{review_queue.processed_path(queue_dir)}[/]"
            )
            console.print()

    pending = review_queue.pending_records(queue_dir)
    _print_queue_summary(pending)
    console.rule(style="dim")
    console.print(
        f"Processed [bold]{len(done)}[/] new  •  "
        f"[red]{failed}[/] failed  •  "
        f"[yellow]{len(pending)}[/] awaiting review"
    )
    if pending:
        console.print(
            "[dim]Run[/] [bold]python -m src.cli review[/] "
            "[dim]to approve / edit / reject drafts.[/]"
        )
    return 0 if failed == 0 else 2


def _cmd_start(args: argparse.Namespace) -> int:
    """Process every new email from a folder of .mbox files into the queue."""
    folder = Path(args.folder)
    if not folder.is_dir():
        console.print(f"[red]not a folder:[/] {folder}")
        return 1
    mbox_files = sorted(folder.glob("*.mbox"))
    if not mbox_files:
        console.print(f"[red]no .mbox files in[/] {folder}")
        return 1

    queue_dir = Path(args.queue_dir)
    already = review_queue.processed_ids(queue_dir)

    emails: list[Email] = []
    sources: dict[str, str] = {}
    skipped = 0
    with console.status("[cyan]Scanning mbox files…", spinner="dots"):
        for mbox_file in mbox_files:
            for email in load_mbox(mbox_file):
                if email.id in already or email.id in sources:
                    skipped += 1
                    continue
                sources[email.id] = f"mbox:{mbox_file.name}"
                emails.append(email)
    if args.limit is not None:
        emails = emails[: args.limit]

    files_noun = "file" if len(mbox_files) == 1 else "files"
    console.print(
        f"[dim]{len(mbox_files)} mbox {files_noun} in [bold]{folder}[/bold] — "
        f"{len(emails)} new email(s) to process, "
        f"{skipped} already processed or duplicate[/]"
    )
    return _run_start_pipeline(emails, sources, args, queue_dir)


def _cmd_start_imap(args: argparse.Namespace) -> int:
    """Process unread IMAP mail into the queue (read-only connection)."""
    queue_dir = Path(args.queue_dir)
    try:
        with console.status(
            f"[cyan]Fetching unread (last {args.days} days) via IMAP…",
            spinner="dots",
        ):
            fetched = load_imap_unread(days=args.days)
    except Exception as exc:
        console.print(f"[red]IMAP error:[/] {exc}")
        return 1

    already = review_queue.processed_ids(queue_dir)
    emails = [e for e in fetched if e.id not in already]
    if args.limit is not None:
        emails = emails[: args.limit]
    sources = {e.id: "imap" for e in emails}
    console.print(
        f"[dim]IMAP returned {len(fetched)} unread email(s); "
        f"{len(emails)} new to process[/]"
    )
    return _run_start_pipeline(emails, sources, args, queue_dir)


def _cmd_reset(args: argparse.Namespace) -> int:
    """Delete the queue ledgers so the next `start` reprocesses everything."""
    queue_dir = Path(args.queue_dir)
    targets = [
        review_queue.processed_path(queue_dir),
        review_queue.reviewed_path(queue_dir),
    ]
    existing = [p for p in targets if p.exists()]
    if not existing:
        console.print(f"[green]Queue is already empty[/] [dim]({queue_dir})[/]")
        return 0

    n_processed = len(review_queue.load_records(queue_dir))
    n_reviewed = len(review_queue.reviewed_ids(queue_dir))
    console.print(
        f"This deletes [bold]{n_processed}[/] processed record(s) and "
        f"[bold]{n_reviewed}[/] review decision(s) from [bold]{queue_dir}[/]; "
        f"the next `start` will reprocess everything."
    )
    console.print("[dim]Approved drafts and session logs are not touched.[/]")

    if not args.yes:
        try:
            confirm = Prompt.ask(
                "  [bold]reset the queue?[/] \\[y]es / \\[n]o",
                choices=["y", "n"],
                default="n",
            )
        except EOFError:
            confirm = "n"
        if confirm != "y":
            console.print("[dim]aborted — nothing deleted[/]")
            return 0

    for path in existing:
        path.unlink()
    console.print(
        f"[green]Queue reset[/] — deleted "
        f"{', '.join(str(p) for p in existing)}"
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    """Interactively review every queued email not yet reviewed."""
    queue_dir = Path(args.queue_dir)
    pending = review_queue.pending_records(queue_dir)
    if not pending:
        console.print("[green]Nothing to review — the queue is empty.[/]")
        console.print(
            "[dim]Run `python -m src.cli start <folder>` (or `start-imap`) "
            "to process new mail.[/]"
        )
        return 0

    out_dir = Path(args.approved_dir)
    session_path = (
        Path(args.sessions_dir) / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    console.print(
        f"[dim]{len(pending)} email(s) to review, most important first  •  "
        f"session log → {session_path}[/]"
    )
    console.print()

    reviewed = approved = 0
    quit_early = False
    for i, rec in enumerate(pending, start=1):
        p = _processed_from_record(rec)
        header = Text(f"[{i}/{len(pending)}]  ", style="bold")
        header.append(f"importance {_format_importance(rec.importance)}", style="bold cyan")
        if rec.importance_reason:
            header.append(f" — {rec.importance_reason}", style="dim")
        console.print(header)
        console.print(_render_process_panel(p, max_body_chars=args.max_chars))

        try:
            action = _prompt_action(bool(p.draft and p.draft.strip()))
        except EOFError:  # piped stdin ran dry — treat like quit
            action = "quit"
        if action == "quit":
            _append_session_record(session_path, _session_record(p, "quit", None))
            quit_early = True
            break

        saved_path: Path | None = None
        draft_to_save = p.draft or ""
        if action == "edit":
            draft_to_save = _edit_draft(draft_to_save)
        if action in ("approve", "edit") and draft_to_save.strip():
            saved_path = _persist_approved(p, draft_to_save, out_dir, rec.source)
            approved += 1
            console.print(f"  [green]saved →[/] {saved_path}")
        elif action == "reject":
            console.print("  [dim]rejected — nothing saved[/]")

        review_queue.append_reviewed(queue_dir, rec.email.id, action, saved_path)
        _append_session_record(session_path, _session_record(p, action, saved_path))
        reviewed += 1
        console.print()

    remaining = len(pending) - reviewed
    console.rule(style="dim")
    note = " (stopped early)" if quit_early else ""
    console.print(
        f"Reviewed [bold]{reviewed}[/]{note}  •  "
        f"[green]{approved}[/] approved  •  "
        f"[yellow]{remaining}[/] still pending"
    )
    console.print(f"[dim]Decisions logged to[/] {session_path}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.cli",
        description="Local email triage CLI.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    triage_parser = subparsers.add_parser(
        "triage-emails",
        help="Triage emails from an mbox file using the local model.",
        description=(
            "Iterate through an .mbox file, run each email through the local "
            "triage classifier, and print a rich-formatted result panel."
        ),
    )
    triage_parser.add_argument("mbox_path", type=str, help="Path to an .mbox file")
    triage_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of emails to process (default: 10)",
    )
    triage_parser.add_argument(
        "--shuffle",
        action="store_true",
        help=(
            "Randomly sample emails from the mbox instead of taking the first N. "
            "Pair with --seed for a reproducible random subset."
        ),
    )
    triage_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for --shuffle (omit for nondeterministic).",
    )
    triage_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to the router config (default: configs/router.yaml)",
    )
    triage_parser.set_defaults(func=_cmd_triage_emails)

    anon_parser = subparsers.add_parser(
        "anonymize-emails",
        help="Anonymize emails and preview the Claude API request body.",
        description=(
            "Iterate through an .mbox file, run each email through the chosen "
            "anonymizer, and print a rich-formatted panel showing the original "
            "message, the anonymized message (placeholders highlighted), the "
            "placeholder → original mapping, and the exact request body that "
            "would be sent to the Claude API on escalation. Nothing is sent."
        ),
    )
    anon_parser.add_argument("mbox_path", type=str, help="Path to an .mbox file")
    anon_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of emails to process (default: 5)",
    )
    anon_parser.add_argument(
        "--anonymizer",
        choices=["regex", "combined", "coref"],
        default="combined",
        help="Anonymization strategy (default: combined = regex + NER)",
    )
    anon_parser.add_argument(
        "--task",
        type=str,
        default=DEFAULT_PREVIEW_TASK,
        help="Task description shown to Claude in the user message",
    )
    anon_parser.add_argument(
        "--max-chars",
        type=int,
        default=800,
        help="Truncate original/anonymized message bodies to this many chars (default: 800)",
    )
    anon_parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Randomly sample emails from the mbox instead of taking the first N.",
    )
    anon_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for --shuffle (omit for nondeterministic).",
    )
    anon_parser.set_defaults(func=_cmd_anonymize_emails)

    def _add_process_args(p: argparse.ArgumentParser) -> None:
        """Arguments shared by ``process`` and ``process-old``."""
        p.add_argument(
            "mbox_path",
            type=str,
            nargs="?",
            default=None,
            help="Path to an .mbox file (required unless --source imap)",
        )
        p.add_argument(
            "--source",
            choices=["mbox", "imap"],
            default="mbox",
            help=(
                "Where to read email from: an .mbox file, or unread mail over a "
                "read-only IMAP connection configured via IMAP_HOST / IMAP_USER / "
                "IMAP_PASS / IMAP_FOLDER (default: mbox)"
            ),
        )
        p.add_argument(
            "--days",
            type=int,
            default=7,
            help="With --source imap: fetch unread from the last N days (default: 7)",
        )
        p.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum number of emails to process (default: 10)",
        )
        p.add_argument(
            "--anonymizer",
            choices=["regex", "combined", "coref"],
            default="combined",
            help="Anonymization strategy for escalations (default: combined = regex + NER)",
        )
        p.add_argument(
            "--task",
            type=str,
            default=DEFAULT_PROCESS_TASK,
            help="Task instruction sent to Claude for escalated emails",
        )
        p.add_argument(
            "--config",
            type=str,
            default=None,
            help="Path to the router config (default: configs/router.yaml)",
        )
        p.add_argument(
            "--approved-dir",
            type=str,
            default="data/approved_drafts",
            help="Directory for approved drafts (default: data/approved_drafts)",
        )
        p.add_argument(
            "--sessions-dir",
            type=str,
            default="logs/sessions",
            help="Directory for per-run decision logs (default: logs/sessions)",
        )
        p.add_argument(
            "--max-chars",
            type=int,
            default=800,
            help="Truncate the displayed original message to this many chars (default: 800)",
        )
        p.add_argument(
            "--no-input",
            action="store_true",
            help="Present and log without prompting (no approve/reject/edit).",
        )
        p.add_argument(
            "--shuffle",
            action="store_true",
            help="Randomly sample emails from the mbox instead of taking the first N.",
        )
        p.add_argument(
            "--seed",
            type=int,
            default=None,
            help="Random seed for --shuffle (omit for nondeterministic).",
        )

    process_parser = subparsers.add_parser(
        "process",
        help="Full pipeline: triage, score sensitivity, delegate escalations, review.",
        description=(
            "Iterate through an .mbox file. Each email is triaged locally, then "
            "scored for sensitivity; escalated emails are anonymized, sent to "
            "Claude, and the reply is rehydrated locally. Every email is presented "
            "with its classification, escalation decision, and draft (tagged "
            "'local' or 'Claude'), then you approve / edit / reject it. Processing "
            "runs on a background thread, so reviewing the first email never "
            "waits for the rest of the batch. Approved drafts are written to "
            "data/approved_drafts/ and every decision is logged to "
            "logs/sessions/<timestamp>.jsonl. Nothing is ever sent."
        ),
    )
    _add_process_args(process_parser)
    process_parser.set_defaults(func=_cmd_process)

    process_old_parser = subparsers.add_parser(
        "process-old",
        help="Like `process`, but fully sequential (process one, review one).",
        description=(
            "The original sequential version of `process`: each email is triaged, "
            "scored, and (if escalated) delegated in the foreground, then reviewed, "
            "before the next email starts. Same flags, same output, same logs."
        ),
    )
    _add_process_args(process_old_parser)
    process_old_parser.set_defaults(func=_cmd_process_old)

    def _add_pipeline_args(p: argparse.ArgumentParser) -> None:
        """Arguments shared by ``start`` and ``start-imap``."""
        p.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process at most N new emails (default: all)",
        )
        p.add_argument(
            "--anonymizer",
            choices=["regex", "combined", "coref"],
            default="combined",
            help="Anonymization strategy for escalations (default: combined = regex + NER)",
        )
        p.add_argument(
            "--task",
            type=str,
            default=DEFAULT_PROCESS_TASK,
            help="Task instruction sent to Claude for escalated emails",
        )
        p.add_argument(
            "--config",
            type=str,
            default=None,
            help="Path to the router config (default: configs/router.yaml)",
        )
        p.add_argument(
            "--queue-dir",
            type=str,
            default=str(review_queue.DEFAULT_QUEUE_DIR),
            help="Directory for the processed/reviewed ledgers (default: data/queue)",
        )

    start_parser = subparsers.add_parser(
        "start",
        help="Process all new emails from a folder of .mbox files into the review queue.",
        description=(
            "Scan a folder for .mbox files and process every email that isn't "
            "already in the queue: triage locally, score sensitivity, delegate "
            "escalations to Claude (anonymized, then rehydrated). The batch is "
            "ranked by importance with one anonymized Claude call and a summary "
            "table is printed, most important first. Review the queue afterwards "
            "with `review`. Nothing is ever sent."
        ),
    )
    start_parser.add_argument(
        "folder",
        type=str,
        nargs="?",
        default="data/inbox",
        help="Folder containing .mbox files (default: data/inbox)",
    )
    _add_pipeline_args(start_parser)
    start_parser.set_defaults(func=_cmd_start)

    start_imap_parser = subparsers.add_parser(
        "start-imap",
        help="Process unread IMAP mail (read-only) into the review queue.",
        description=(
            "Fetch unread messages from the last N days over a read-only IMAP "
            "connection (configured via IMAP_HOST, IMAP_USER, IMAP_PASS, and "
            "optionally IMAP_FOLDER — use an app-specific password, never the "
            "main account password), then process and rank them exactly like "
            "`start`. Never marks read, never deletes, never sends."
        ),
    )
    start_imap_parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Fetch unread from the last N days (default: 7)",
    )
    _add_pipeline_args(start_imap_parser)
    start_imap_parser.set_defaults(func=_cmd_start_imap)

    review_parser = subparsers.add_parser(
        "review",
        help="Interactively review queued emails, most important first.",
        description=(
            "Walk every processed-but-unreviewed email in the queue, most "
            "important first, and approve / edit / reject each draft. Approved "
            "drafts are written to data/approved_drafts/; every decision is "
            "appended to the reviewed ledger and the session log. Quit anytime "
            "with q — the rest stays queued."
        ),
    )
    review_parser.add_argument(
        "--approved-dir",
        type=str,
        default="data/approved_drafts",
        help="Directory for approved drafts (default: data/approved_drafts)",
    )
    review_parser.add_argument(
        "--sessions-dir",
        type=str,
        default="logs/sessions",
        help="Directory for per-run decision logs (default: logs/sessions)",
    )
    review_parser.add_argument(
        "--max-chars",
        type=int,
        default=800,
        help="Truncate the displayed original message to this many chars (default: 800)",
    )
    review_parser.add_argument(
        "--queue-dir",
        type=str,
        default=str(review_queue.DEFAULT_QUEUE_DIR),
        help="Directory for the processed/reviewed ledgers (default: data/queue)",
    )
    review_parser.set_defaults(func=_cmd_review)

    reset_parser = subparsers.add_parser(
        "reset",
        help="Reset the review queue so the next `start` reprocesses everything.",
        description=(
            "Delete the queue ledgers (processed.jsonl and reviewed.jsonl) under "
            "the queue directory, so the next `start` run treats every email as "
            "new. Approved drafts in data/approved_drafts/ and session logs are "
            "not touched. Mainly for testing."
        ),
    )
    reset_parser.add_argument(
        "--queue-dir",
        type=str,
        default=str(review_queue.DEFAULT_QUEUE_DIR),
        help="Directory for the processed/reviewed ledgers (default: data/queue)",
    )
    reset_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    reset_parser.set_defaults(func=_cmd_reset)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
