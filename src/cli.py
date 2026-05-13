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
import random
import sys
from itertools import islice
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

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

ESCALATE_CONFIDENCE_THRESHOLD = 0.6


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
