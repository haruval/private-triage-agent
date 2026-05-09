#!/usr/bin/env python3
"""Run the triage pipeline on the dev corpus and emit a markdown verification report.

Same code paths as `python -m src.cli triage-emails`, but additionally captures
the full email body alongside the structured triage result so you can manually
audit whether the local model is calling things correctly.

Output: reports/triage_verification.md
"""

from __future__ import annotations

import sys
from collections import Counter
from itertools import islice
from pathlib import Path

from rich.console import Console

from src.cli import _render_email_panel, _should_escalate
from src.ingestion.mbox_loader import load_mbox
from src.triage.classifier import triage
from src.triage.ollama_client import OllamaClient

LIMIT = 25
MBOX_PATH = Path("data/dev_corpus.mbox")
REPORT_PATH = Path("reports/triage_verification.md")

console = Console()


def _safe_anchor(i: int, subject: str) -> str:
    """A markdown-friendly anchor slug for a numbered email entry."""
    return f"#{i}-{''.join(c if c.isalnum() else '-' for c in subject.lower())[:40].strip('-')}"


def main() -> int:
    if not MBOX_PATH.exists():
        console.print(f"[red]No mbox at {MBOX_PATH}[/]")
        return 1

    client = OllamaClient()
    console.print(
        f"[dim]Loading {MBOX_PATH} (limit {LIMIT}, model {client.model})…[/]\n"
    )
    emails = list(islice(load_mbox(MBOX_PATH), LIMIT))
    if not emails:
        console.print("[red]No emails found in mbox[/]")
        return 1

    triple: list[tuple] = []
    for i, email in enumerate(emails, 1):
        subject_preview = (email.subject or "(no subject)")[:60]
        try:
            with console.status(
                f"[cyan]Triaging [{i}/{len(emails)}] {subject_preview}",
                spinner="dots",
            ):
                result = triage(email, client=client)
            triple.append((email, result, None))
            console.print(_render_email_panel(email, result, _should_escalate(result)))
        except Exception as exc:
            triple.append((email, None, exc))
            console.print(f"[red]Error on email {i}: {exc}[/]")

    # ---- markdown report -------------------------------------------------
    md: list[str] = []
    md.append("# Triage Verification Report\n\n")
    md.append(
        f"Source: `{MBOX_PATH}`  ·  Sample: {len(emails)} emails  ·  "
        f"Model: `{client.model}`\n\n"
    )

    counts: Counter[str] = Counter()
    for _, result, _ in triple:
        if result:
            counts[result.category] += 1
    failures = sum(1 for _, _, exc in triple if exc)

    md.append("## Category breakdown\n\n")
    for cat, n in counts.most_common():
        md.append(f"- **{cat}**: {n}\n")
    if failures:
        md.append(f"- **failed**: {failures}\n")
    md.append("\n---\n\n")

    for i, (email, result, exc) in enumerate(triple, 1):
        subject = (email.subject or "(no subject)").strip() or "(no subject)"
        md.append(f"## {i}. {subject}\n\n")
        md.append(f"- **From:** `{email.from_addr}`\n")
        md.append(f"- **Date:** {email.date.isoformat()}\n")
        md.append(f"- **Message-ID:** `{email.id}`\n\n")

        body = email.body_plain or "(empty)"
        md.append("### Body\n\n```\n")
        md.append(body)
        if not body.endswith("\n"):
            md.append("\n")
        md.append("```\n\n")

        if exc is not None:
            md.append("### Triage result: ERROR\n\n")
            md.append(f"```\n{type(exc).__name__}: {exc}\n```\n\n")
        else:
            md.append("### Triage result\n\n")
            md.append(f"- **Category:** `{result.category}`\n")
            md.append(f"- **Confidence:** {result.confidence:.2f}\n")
            md.append(f"- **Summary:** {result.summary}\n")
            if result.extracted_action_items:
                md.append("- **Action items:**\n")
                for item in result.extracted_action_items:
                    md.append(f"  - {item}\n")
            else:
                md.append("- **Action items:** _(none)_\n")
            if result.suggested_reply_draft:
                md.append("- **Suggested reply:**\n  > ")
                md.append(result.suggested_reply_draft.replace("\n", "\n  > "))
                md.append("\n")
            md.append(f"- **Reasoning:** {result.reasoning}\n")
            escalate = _should_escalate(result)
            md.append(f"- **Escalate flag:** `{escalate}`\n")

        md.append("\n---\n\n")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("".join(md))
    console.print(
        f"\n[green]Wrote report[/] [bold]{REPORT_PATH}[/] "
        f"[dim]({REPORT_PATH.stat().st_size:,} bytes)[/]"
    )
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
