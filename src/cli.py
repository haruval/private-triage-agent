"""End-to-end CLI for the email triage pipeline.

Usage:
    python -m src.cli triage-emails    data/dev_corpus.mbox --limit 10
    python -m src.cli anonymize-emails data/dev_corpus.mbox --limit 5
    python -m src.cli process          data/dev_corpus.mbox --limit 10

``triage-emails`` runs the local model and prints a result panel per email.
``anonymize-emails`` previews exactly what would leave the box on escalation.
``process`` is the full loop: triage locally → score sensitivity → for the
escalated emails, anonymize, delegate to Claude, and rehydrate the reply →
present everything for human approve/reject/edit. Nothing is ever sent
automatically; approved drafts are written to ``data/approved_drafts/`` and
every decision is appended to ``logs/sessions/<timestamp>.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from src.anonymize.ner_anonymizer import CombinedAnonymizer
from src.anonymize.regex_anonymizer import RegexAnonymizer
from src.anonymize.rehydrate import rehydrate
from src.delegate.claude_client import ClaudeClient
from src.ingestion.mbox_loader import Email, load_mbox
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
# Subcommand: process  (full pipeline — triage, escalate, delegate, review)
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


def _select_emails(
    mbox_path: Path, limit: int, shuffle: bool, seed: int | None
) -> list[Email]:
    """Pick emails from the mbox: first N, or a (optionally seeded) random N."""
    if shuffle:
        all_emails = list(load_mbox(mbox_path))
        rng = random.Random(seed)
        if limit < len(all_emails):
            chosen = rng.sample(all_emails, limit)
        else:
            chosen = all_emails
            rng.shuffle(chosen)
        seed_note = f", seed {seed}" if seed is not None else ""
        console.print(
            f"[dim](shuffled {len(chosen)} of {len(all_emails)} emails{seed_note})[/]"
        )
        return chosen
    return list(islice(load_mbox(mbox_path), limit))


def _cmd_process(args: argparse.Namespace) -> int:
    mbox_path = Path(args.mbox_path)
    if not mbox_path.exists():
        console.print(f"[red]mbox not found:[/] {mbox_path}")
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
        f"[dim]Processing[/] [bold]{mbox_path}[/] "
        f"[dim](limit {args.limit}, anonymizer {anon_label}, "
        f"triage {ollama_client.model})[/]"
    )
    if not interactive:
        console.print("[dim](non-interactive: presenting + logging only, no prompts)[/]")
    console.print(f"[dim]session log → {session_path}[/]")
    console.print()

    emails = _select_emails(mbox_path, args.limit, args.shuffle, args.seed)

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
                saved_path = _save_approved_draft(p, draft_to_save, out_dir)
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

    process_parser = subparsers.add_parser(
        "process",
        help="Full pipeline: triage, score sensitivity, delegate escalations, review.",
        description=(
            "Iterate through an .mbox file. Each email is triaged locally, then "
            "scored for sensitivity; escalated emails are anonymized, sent to "
            "Claude, and the reply is rehydrated locally. Every email is presented "
            "with its classification, escalation decision, and draft (tagged "
            "'local' or 'Claude'), then you approve / edit / reject it. Approved "
            "drafts are written to data/approved_drafts/ and every decision is "
            "logged to logs/sessions/<timestamp>.jsonl. Nothing is ever sent."
        ),
    )
    process_parser.add_argument("mbox_path", type=str, help="Path to an .mbox file")
    process_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of emails to process (default: 10)",
    )
    process_parser.add_argument(
        "--anonymizer",
        choices=["regex", "combined", "coref"],
        default="combined",
        help="Anonymization strategy for escalations (default: combined = regex + NER)",
    )
    process_parser.add_argument(
        "--task",
        type=str,
        default=DEFAULT_PROCESS_TASK,
        help="Task instruction sent to Claude for escalated emails",
    )
    process_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to the router config (default: configs/router.yaml)",
    )
    process_parser.add_argument(
        "--approved-dir",
        type=str,
        default="data/approved_drafts",
        help="Directory for approved drafts (default: data/approved_drafts)",
    )
    process_parser.add_argument(
        "--sessions-dir",
        type=str,
        default="logs/sessions",
        help="Directory for per-run decision logs (default: logs/sessions)",
    )
    process_parser.add_argument(
        "--max-chars",
        type=int,
        default=800,
        help="Truncate the displayed original message to this many chars (default: 800)",
    )
    process_parser.add_argument(
        "--no-input",
        action="store_true",
        help="Present and log without prompting (no approve/reject/edit).",
    )
    process_parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Randomly sample emails from the mbox instead of taking the first N.",
    )
    process_parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for --shuffle (omit for nondeterministic).",
    )
    process_parser.set_defaults(func=_cmd_process)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
