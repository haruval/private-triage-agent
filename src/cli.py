"""End-to-end CLI for the email triage pipeline.

Usage:
    python -m src.cli triage-emails data/dev_corpus.mbox --limit 10

Loads emails from an mbox file, runs each through the local Ollama-based
triage, and prints a rich-formatted summary panel per email.

Anonymization and Claude delegation are stubbed for now — emails that the
local model is uncertain about get an ``escalate`` flag set, but nothing
downstream acts on it yet.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from itertools import islice
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from src.anonymize.ner_anonymizer import CombinedAnonymizer
from src.anonymize.regex_anonymizer import RegexAnonymizer
from src.ingestion.mbox_loader import Email, load_mbox
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

# Escalate when the local model is anything short of confident. gemma3:27b
# rarely reports confidence below ~0.75 on this corpus, so a 0.6 bar almost
# never fired; 0.8 routes the genuinely uncertain cases to Claude.
ESCALATE_CONFIDENCE_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Stubs for the escalation path
# ---------------------------------------------------------------------------


def _should_escalate(result: TriageResult) -> bool:
    """Decide whether this email should go to Claude (after anonymization).

    Currently triggers for low-confidence outputs or ``unclear`` category —
    cases where the local model didn't have enough signal. Not yet acted on.
    """
    return (
        result.confidence < ESCALATE_CONFIDENCE_THRESHOLD
        or result.category == "unclear"
    )


def _anonymize_and_delegate_stub(email: Email, result: TriageResult) -> None:
    """Placeholder for the escalation pipeline.

    When implemented, this will:
      1. anonymize(email)        -> AnonymizedEmail (PII replaced with tokens)
      2. delegate(anonymized)    -> Claude API response
      3. rehydrate(response)     -> result with original PII restored locally

    For now, called for the side effect of marking emails — the flag
    propagates into the rendered panel but nothing leaves the local machine.
    """
    # TODO: implement src/anonymize/ — name/email/phone tokenization + mapping store
    # TODO: implement src/delegate/ — Claude API client with caching
    # TODO: implement src/anonymize/rehydrate — re-substitute tokens in Claude's reply
    return None


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


def _render_email_panel(email: Email, result: TriageResult, escalate: bool) -> Panel:
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
    if escalate:
        body.append("true", style="bold red")
        body.append("  (would route to Claude — not wired yet)", style="dim")
    else:
        body.append("false", style="dim")

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

        escalate = _should_escalate(result)
        if escalate:
            escalated += 1
            _anonymize_and_delegate_stub(email, result)

        console.print(_render_email_panel(email, result, escalate))
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
