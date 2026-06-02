"""Utility eval: does anonymization preserve enough meaning for Claude?

We take a handful of escalate-worthy emails and draft a reply three ways:

    (a) raw        — original email → Claude                 (utility ceiling;
                     this sends real PII to the API, on purpose, as the
                     baseline the anonymized pipelines are measured against)
    (b) regex      — regex-anonymized → Claude → rehydrate
    (c) full       — regex+NER+coref → Claude → rehydrate

Each rehydrated draft is then scored by the local judge (gemma3:27b) on a
1–5 rubric: relevance, specificity, actionability, naturalness. The judge
sees the *original* email and the *rehydrated* draft, and is blind to which
pipeline produced it.

LLM-as-judge is noisy — that's expected. The point is a rough, spot-checkable
signal on how much utility each anonymization strategy costs, not a formal
statistic. Per-pipeline-per-email records land in
``logs/utility_eval_<timestamp>.jsonl`` for hand auditing; a summary of mean
scores is printed as a rich table.

Run:
    python -m src.eval.utility_eval --num-emails 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rich.console import Console
from rich.table import Table

from src.anonymize.rehydrate import rehydrate
from src.ingestion.mbox_loader import Email, load_mbox

console = Console()

# Pipeline ids and their human-readable labels for the summary table.
PIPELINES: tuple[str, ...] = ("raw", "regex", "full")
PIPELINE_LABELS: dict[str, str] = {
    "raw": "(a) raw",
    "regex": "(b) regex",
    "full": "(c) full",
}
# Spelled out under the table so the short row labels stay aligned.
PIPELINE_CAPTION = (
    "(a) raw → Claude   "
    "(b) regex-anon → Claude → rehydrate   "
    "(c) regex+NER+coref → Claude → rehydrate"
)

RUBRIC_AXES: tuple[str, ...] = ("relevance", "specificity", "actionability", "naturalness")

DEFAULT_MBOX = Path("data/dev_corpus.mbox")
DEFAULT_TASK = "Draft a concise, professional reply to the email below."
DEFAULT_NUM_EMAILS = 10
MAX_BODY_CHARS = 3000


# ---------------------------------------------------------------------------
# Judge rubric
# ---------------------------------------------------------------------------


JUDGE_SYSTEM_PROMPT = (
    "You are a meticulous evaluator of email reply drafts. You score a draft "
    "against the email it responds to. Respond with ONLY a JSON object — no "
    "prose, no code fences."
)

JUDGE_PROMPT_TEMPLATE = """\
Score the candidate reply draft below against the original email. Use a 1–5
integer scale on each axis (1 = poor, 5 = excellent):

- relevance: does the draft address what the email actually says and wants?
- specificity: does it reference concrete details (names, dates, the actual
  ask) instead of generic filler?
- actionability: does it give the recipient a clear way to move forward?
- naturalness: does it read like fluent, human-written email?

ORIGINAL EMAIL:
{email}

CANDIDATE REPLY DRAFT:
{draft}

Respond with ONLY this JSON object:
{{"relevance": <1-5>, "specificity": <1-5>, "actionability": <1-5>, "naturalness": <1-5>, "justification": "<one short sentence>"}}
"""


@dataclass
class RubricScores:
    relevance: float
    specificity: float
    actionability: float
    naturalness: float
    justification: str = ""

    @property
    def mean(self) -> float:
        return (
            self.relevance + self.specificity + self.actionability + self.naturalness
        ) / 4.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "relevance": self.relevance,
            "specificity": self.specificity,
            "actionability": self.actionability,
            "naturalness": self.naturalness,
            "justification": self.justification,
        }

    @classmethod
    def from_json_dict(cls, d: dict[str, Any]) -> "RubricScores":
        """Validate a judge's JSON response. Raises ValueError on bad shape."""
        if not isinstance(d, dict):
            raise ValueError(f"Expected a dict, got {type(d).__name__}")

        def _axis(name: str) -> float:
            if name not in d:
                raise ValueError(f"Missing required score: {name!r}")
            v = d[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{name!r} must be a number, got {type(v).__name__}")
            v = float(v)
            if not 1.0 <= v <= 5.0:
                raise ValueError(f"{name!r} must be in [1, 5], got {v}")
            return v

        justification = d.get("justification", "")
        if not isinstance(justification, str):
            justification = str(justification)

        return cls(
            relevance=_axis("relevance"),
            specificity=_axis("specificity"),
            actionability=_axis("actionability"),
            naturalness=_axis("naturalness"),
            justification=justification,
        )


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    pipeline: str
    draft: str | None
    num_placeholders: int
    scores: RubricScores | None
    error: str | None = None


@dataclass
class EmailEvalResult:
    email_id: str
    subject: str
    results: list[PipelineResult]


# ---------------------------------------------------------------------------
# Email formatting
# ---------------------------------------------------------------------------


def _email_text(email: Email, max_body_chars: int = MAX_BODY_CHARS) -> str:
    """Render an email to the plain text that gets anonymized / sent / judged.

    The same rendering is used for all three pipelines so the comparison is
    apples-to-apples — only the anonymization step differs between them.
    """
    body = email.body_plain or ""
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n[...truncated...]"
    return (
        f"Subject: {email.subject or '(no subject)'}\n"
        f"From: {email.from_addr or '(unknown)'}\n"
        f"\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# Email selection
# ---------------------------------------------------------------------------


def select_escalation_emails(
    emails: Iterable[Email],
    *,
    client: Any,
    num_emails: int = DEFAULT_NUM_EMAILS,
    scan_limit: int | None = None,
) -> list[Email]:
    """Triage emails and keep the ones the router would escalate.

    Reuses ``_should_escalate`` from the CLI so selection matches what the
    rest of the system considers escalate-worthy. Stops once ``num_emails``
    are collected (or ``scan_limit`` emails have been triaged).
    """
    # Imported here to avoid a hard import cycle and to keep the heavy triage
    # path out of the module import for tests that inject pre-selected emails.
    from src.cli import _should_escalate
    from src.triage.classifier import triage

    selected: list[Email] = []
    for i, email in enumerate(emails):
        if scan_limit is not None and i >= scan_limit:
            break
        try:
            result = triage(email, client=client)
        except Exception as exc:  # a single bad email shouldn't abort selection
            console.print(f"[dim]skipped {email.id}: triage failed ({exc})[/]")
            continue
        if _should_escalate(result):
            selected.append(email)
            if len(selected) >= num_emails:
                break
    return selected


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------


def _draft_for_pipeline(
    email_text: str,
    pipeline: str,
    *,
    claude: Any,
    regex_anon: Any,
    full_anon: Any,
    task: str,
) -> tuple[str, int]:
    """Run one pipeline. Returns (rehydrated_draft, num_placeholders)."""
    if pipeline == "raw":
        draft = claude.delegate(email_text, None, task)
        return draft, 0

    if pipeline == "regex":
        anonymized, mapping = regex_anon.anonymize(email_text)
    elif pipeline == "full":
        anonymized, mapping = full_anon.anonymize(email_text)
    else:
        raise ValueError(f"unknown pipeline: {pipeline!r}")

    draft = claude.delegate(anonymized, None, task)
    return rehydrate(draft, mapping), len(mapping)


def _judge_draft(judge: Any, email_text: str, draft: str) -> RubricScores:
    prompt = JUDGE_PROMPT_TEMPLATE.format(email=email_text, draft=draft)
    raw = judge.generate_json(prompt=prompt, system=JUDGE_SYSTEM_PROMPT)
    return RubricScores.from_json_dict(raw)


# ---------------------------------------------------------------------------
# Eval driver
# ---------------------------------------------------------------------------


def _default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"utility_eval_{stamp}.jsonl"


def run_utility_eval(
    emails: list[Email],
    *,
    claude: Any,
    judge: Any,
    regex_anon: Any = None,
    full_anon: Any = None,
    task: str = DEFAULT_TASK,
    log_path: Path | str | None = None,
) -> tuple[list[EmailEvalResult], Path]:
    """Run all three pipelines over ``emails`` and judge each draft.

    External clients are injected so this is fully testable with mocks:
      - ``claude``: object with ``.delegate(email, thread, task) -> str`` and ``.model``
      - ``judge``:  object with ``.generate_json(prompt, system) -> dict`` and ``.model``

    Per-pipeline failures (a Claude error, an unparseable judge response) are
    captured on the result rather than aborting the whole run.
    """
    if regex_anon is None:
        from src.anonymize.regex_anonymizer import RegexAnonymizer
        regex_anon = RegexAnonymizer()
    if full_anon is None:
        from src.anonymize.coref_anonymizer import CorefAnonymizer
        full_anon = CorefAnonymizer()

    out_path = Path(log_path) if log_path else _default_log_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    claude_model = getattr(claude, "model", None)
    judge_model = getattr(judge, "model", None)

    results: list[EmailEvalResult] = []
    with out_path.open("a") as logf:
        for email in emails:
            email_text = _email_text(email)
            pipeline_results: list[PipelineResult] = []

            for pipeline in PIPELINES:
                try:
                    draft, n_placeholders = _draft_for_pipeline(
                        email_text,
                        pipeline,
                        claude=claude,
                        regex_anon=regex_anon,
                        full_anon=full_anon,
                        task=task,
                    )
                except Exception as exc:
                    pr = PipelineResult(pipeline, None, 0, None, error=f"delegate: {exc}")
                    pipeline_results.append(pr)
                    _write_log(logf, email, pr, task, claude_model, judge_model)
                    continue

                try:
                    scores: RubricScores | None = _judge_draft(judge, email_text, draft)
                    error = None
                except Exception as exc:
                    scores = None
                    error = f"judge: {exc}"

                pr = PipelineResult(pipeline, draft, n_placeholders, scores, error=error)
                pipeline_results.append(pr)
                _write_log(logf, email, pr, task, claude_model, judge_model)

            results.append(
                EmailEvalResult(
                    email_id=email.id,
                    subject=email.subject or "(no subject)",
                    results=pipeline_results,
                )
            )

    return results, out_path


def _write_log(
    logf: Any,
    email: Email,
    pr: PipelineResult,
    task: str,
    claude_model: str | None,
    judge_model: str | None,
) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "email_id": email.id,
        "subject": email.subject,
        "pipeline": pr.pipeline,
        "task": task,
        "num_placeholders": pr.num_placeholders,
        "draft": pr.draft,
        "scores": pr.scores.as_dict() if pr.scores else None,
        "error": pr.error,
        "claude_model": claude_model,
        "judge_model": judge_model,
    }
    logf.write(json.dumps(record) + "\n")
    logf.flush()


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------


def build_summary_table(results: list[EmailEvalResult]) -> Table:
    table = Table(
        title="Utility eval — mean judge scores by pipeline (1–5)",
        caption=PIPELINE_CAPTION,
        caption_justify="left",
    )
    table.add_column("pipeline", style="bold", no_wrap=True)
    for axis in RUBRIC_AXES:
        table.add_column(axis, justify="right")
    table.add_column("mean", justify="right", style="bold")
    table.add_column("n", justify="right", style="dim")
    table.add_column("errors", justify="right", style="dim")

    for pipeline in PIPELINES:
        prs = [pr for r in results for pr in r.results if pr.pipeline == pipeline]
        scored = [pr.scores for pr in prs if pr.scores is not None]
        errors = sum(1 for pr in prs if pr.error is not None)

        if not scored:
            row = [PIPELINE_LABELS[pipeline]] + ["—"] * (len(RUBRIC_AXES) + 1)
            row += [str(len(scored)), str(errors)]
            table.add_row(*row)
            continue

        axis_means = {
            axis: sum(getattr(s, axis) for s in scored) / len(scored)
            for axis in RUBRIC_AXES
        }
        overall = sum(s.mean for s in scored) / len(scored)
        row = [PIPELINE_LABELS[pipeline]]
        row += [f"{axis_means[axis]:.2f}" for axis in RUBRIC_AXES]
        row += [f"{overall:.2f}", str(len(scored)), str(errors)]
        table.add_row(*row)

    return table


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="src.eval.utility_eval",
        description=(
            "Measure how much utility anonymization costs: draft replies to "
            "escalate-worthy emails three ways (raw / regex / full pipeline) "
            "and score them with a local judge."
        ),
    )
    parser.add_argument("--mbox", type=str, default=str(DEFAULT_MBOX),
                        help=f"Path to the mbox file (default: {DEFAULT_MBOX})")
    parser.add_argument("--num-emails", type=int, default=DEFAULT_NUM_EMAILS,
                        help=f"How many escalate-worthy emails to evaluate (default: {DEFAULT_NUM_EMAILS})")
    parser.add_argument("--scan-limit", type=int, default=None,
                        help="Stop scanning the mbox after this many emails when selecting (default: no limit)")
    parser.add_argument("--task", type=str, default=DEFAULT_TASK,
                        help="Task instruction passed to Claude")
    args = parser.parse_args(argv)

    mbox_path = Path(args.mbox)
    if not mbox_path.exists():
        console.print(f"[red]mbox not found:[/] {mbox_path}")
        return 1

    from src.config import load_env_file
    load_env_file()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/] The raw and anonymized "
            "pipelines both call the Claude API; export a key and retry."
        )
        return 1

    from src.delegate.claude_client import ClaudeClient
    from src.triage.ollama_client import OllamaClient

    ollama = OllamaClient()
    claude = ClaudeClient()

    console.print(
        f"[dim]Selecting up to {args.num_emails} escalate-worthy emails from "
        f"{mbox_path} (triage model {ollama.model})…[/]"
    )
    with console.status("[cyan]Triaging for escalation candidates", spinner="dots"):
        emails = select_escalation_emails(
            load_mbox(mbox_path),
            client=ollama,
            num_emails=args.num_emails,
            scan_limit=args.scan_limit,
        )

    if not emails:
        console.print(
            "[yellow]No escalate-worthy emails found.[/] Try a larger "
            "--scan-limit or lower the escalation threshold in src/cli.py."
        )
        return 0

    console.print(
        f"[dim]Evaluating {len(emails)} emails × {len(PIPELINES)} pipelines "
        f"(Claude {claude.model}, judge {ollama.model})…[/]\n"
    )

    results, log_path = run_utility_eval(
        emails, claude=claude, judge=ollama, task=args.task
    )

    console.print(build_summary_table(results))
    console.print(f"\n[dim]Wrote per-draft records to[/] [bold]{log_path}[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
