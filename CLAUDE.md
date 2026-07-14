# CLAUDE.md

Working notes for Claude when extending this project. The goal is to keep new
code consistent with the patterns laid down in weeks 1–2 and to move the
roadmap forward without re-litigating decisions that are already settled.

## Project goal

A privacy-preserving email triage agent.

- **Local model** (`gemma3:27b` via Ollama) handles every email by default —
  classification, summaries, action-item extraction, and reply drafts.
- **Sensitive content** gets anonymized before being sent to Claude API for
  harder reasoning, then re-hydrated locally.
- **Nothing sends automatically.** Drafts are reviewed by a human before they
  leave the box.

## Layout

```
src/ingestion/    mbox + (later) IMAP loaders → Email dataclass
src/triage/       Ollama client + local-model classifier
src/anonymize/    regex / NER / coref anonymizers + (next) rehydrate
src/router/       (next) sensitivity scoring + escalation
src/delegate/     (next) Claude API client
src/eval/         corpus loader, leak detector, (next) utility eval
tests/            pytest
scripts/          one-off runners (eval_pronoun_leak.py, fetch_enron.py, …)
data/             gitignored — corpora, mbox files, eval JSONL
configs/          YAML config (router weights live here once it exists)
logs/             JSONL call logs (ollama_calls.jsonl, claude_calls.jsonl)
reports/          markdown writeups (triage_verification.md, final_eval.md)
```

Plain `pip` + `venv`. `make install` builds the venv and downloads
`en_core_web_trf`. Python 3.12 on Apple Silicon is the target.

## State of the build

The original prompt pack drives this project (17 prompts). **Prompts 1–10 are
done.** That means you can rely on:

- `Email` dataclass and `load_mbox()` (handles multipart, RFC 2047, dedupe)
- `OllamaClient` with `generate()` / `generate_json()` (retry + JSONL logging)
- `triage(email) -> TriageResult` using few-shot prompting
- `python -m src.cli triage-emails …` end-to-end CLI with rich panels (no
  delegation yet — escalate flag is computed but unused)
- `data/eval/sensitive_spans.jsonl` (15 hand-labeled rows)
- `detect_leaks()` + `LeakReport`
- `RegexAnonymizer`, `NERAnonymizer`, `CombinedAnonymizer` (regex first,
  then NER on the regex-anonymized text)
- `CorefAnonymizer` (fastcoref, see below) + `scripts/eval_pronoun_leak.py`

**Prompts 11–16 are done too** — rehydration, utility eval, sensitivity
scorer, Claude client, full integration (`process` runs its processing on a
background thread; `process-old` is the original sequential version), and
read-only IMAP (`src/ingestion/imap_loader.py`; `--source imap` on both
process commands). Beyond the prompt pack there is a queue-based pipeline:
`start` / `start-imap` process all new mail into append-only ledgers under
`data/queue/` (`src/review_queue.py`), one anonymized Claude call ranks each
batch by importance (`src/router/importance.py`), and `review` walks the
unreviewed queue most-important-first. The planned single entry point (first
run asks: local mbox files vs. connect email over IMAP) is not built yet —
`start` and `start-imap` are deliberately separate commands for now.

**Remaining: prompt 17** (final eval & writeup). The per-prompt notes below
are kept for reference.

## Conventions to preserve

These patterns are load-bearing — keep new code consistent.

**Module docstring.** Every module opens with a short paragraph explaining
what it does and what trade-offs were made. Tone is matter-of-fact, not
marketing. The regex and NER anonymizer module docstrings are good
templates.

**Dataclasses for typed values.** Domain objects are dataclasses (`Email`,
`TriageResult`, `Detection`, `SensitiveSpan`, `EvalExample`, `LeakReport`).
Use `@dataclass(frozen=True)` only when the value is truly immutable (e.g.
`Detection`, `_RawHit`). New domain types should follow suit.

**Validation via classmethods.** When parsing untrusted shapes (JSON from
the model, JSON from disk), prefer a `from_json_dict()` classmethod that
raises `ValueError` with a clear message — `TriageResult.from_json_dict` is
the canonical example. Don't sprinkle ad-hoc `dict.get(...)` validation.

**Public API mirrors.** Each anonymizer exposes the same two methods —
`detect(text) -> list[Detection]` and `anonymize(text) -> tuple[str, dict]`.
`Detection` lives in `regex_anonymizer.py` and is shared. New anonymizers
(or wrappers) match this surface.

**Placeholders read as proper nouns.** Always `Prefix_LetterN` —
`Alex_P1`, `Acme_O1`, `Email_E1`, `Date_D1`, `Address_A1`. Sequential per
letter, within a document. Same value always maps to the same placeholder.
This shape matters because downstream LLMs treat the placeholders as
in-distribution proper nouns.

**Replacement order.** Apply right-to-left so character offsets stay valid.
Resolve overlapping spans by preferring longer matches first, then earlier
starts. `_resolve_overlaps` in `ner_anonymizer.py` is the reference.

**Logging.** Every external call (Ollama, later Claude) writes one JSON line
to `logs/<system>_calls.jsonl` with timestamp, latency_ms, input/output
sizes, model. Follow `ollama_client.py`'s shape.

**Tests.** Use the eval corpus where it makes sense — `test_regex_anonymizer.py`
and `test_ner_anonymizer.py` both compute precision/recall against
`sensitive_spans.jsonl` and print a table at the end of the run. Mock external
clients (`ollama`, `anthropic`) — don't hit the network in unit tests.

**Imports.** `from __future__ import annotations` at the top of every module
(we lean on PEP 604 `X | None` syntax). Lazy-import heavy deps (`spacy`,
`fastcoref`, `anthropic`) inside `__init__` so tests can skip them.

**No new abstractions until you have a second caller.** Resist the urge to
factor early — every existing module is one concrete thing.

## Library / install gotchas

- **`fastcoref`** (for coref) needs `transformers<5` pinned —
  `FCorefModel.all_tied_weights_keys` was renamed in transformers 5.x. Both
  pins are in `requirements.txt`.
- **`spacy-experimental`** does NOT build on Python 3.12 / Apple Silicon
  (uses removed `_PyCFrame->use_tracing`). Don't reach for it again.
- **`en_core_web_trf`** is downloaded by `make install`; if missing,
  `NERAnonymizer` raises with the install command.
- **Package-age guard.** `scripts/check_package_ages.py` runs from the
  Makefile and rejects packages newer than the floor date. Bypass with
  `ALLOW_RECENT_PACKAGES=1 make install` when needed.

## Remaining roadmap (prompts 11–17)

Each item below is a faithful summary of the original prompt with extra
context for what to be careful about given the code that already exists.

### Prompt 11 — Rehydration (`src/anonymize/rehydrate.py`)

`rehydrate(text, mapping) -> str` is the inverse of `anonymize()`. Cases to
handle:

- Possessives — `Alex_P1's` → `Sarah's`. Strip the `'s` before lookup.
- Punctuation-adjacent placeholders — `(Alex_P1)`, `Alex_P1,`, etc. Regex
  on the placeholder shape `[A-Z][a-z]+_[A-Z]\d+` is fine.
- Unknown placeholder from Claude (not in mapping) — leave it as-is, log a
  warning. Don't raise.
- Claude paraphrases — `the person Alex_P1` — substitute the placeholder
  normally; the surrounding words are Claude's job, not ours.

Tests should hit each of those cases plus the round-trip
`rehydrate(anonymize(text)) == text` for the eval corpus.

### Prompt 12 — Utility eval (`src/eval/utility_eval.py`)

For ~10 escalate-worthy emails, run three pipelines:
(a) raw → Claude (b) regex-anonymized → Claude → rehydrate
(c) full (regex+NER+coref) → Claude → rehydrate. Score the three drafts
with `gemma3:27b` as judge (relevance / specificity / actionability /
naturalness, 1–5 each). Save to `logs/utility_eval_<timestamp>.jsonl`.
Print a `rich.table.Table` summary.

This depends on prompts 11 and 14 (rehydrate + Claude client). LLM-as-judge
is noisy on purpose — design for spot-checking, not formal stats.

### Prompt 13 — Sensitivity scorer (`src/router/sensitivity_scorer.py`)

`EscalationDecision(escalate: bool, reason: str, score: float)`. Inputs:
`Email` + `TriageResult`. Combine: low triage confidence, keyword presence
(legal / negotiation / technical / sensitive — configurable lists), thread
length, explicit always-escalate sender override. **All weights in
`configs/router.yaml`** loaded with `yaml.safe_load`. This replaces the
inline `_should_escalate` stub in `src/cli.py:49`.

### Prompt 14 — Claude delegation (`src/delegate/claude_client.py`)

`delegate(anonymized_email, anonymized_thread, task) -> str` using the
`anthropic` SDK. **Verify the current model string from the SDK docs or
the API; do not guess from memory.** System prompt must instruct Claude
to preserve `Name_P1`-style placeholders verbatim. Log every call to
`logs/claude_calls.jsonl` with token counts and latency. Exponential
backoff on rate limits.

### Prompt 15 — Full integration (extend `src/cli.py`)

New `process` command: triage → score sensitivity → if escalate: anonymize,
delegate, rehydrate → present with rich. Show original, classification,
escalation decision, draft + provenance (`local` vs `Claude`), and an
approve/reject/edit prompt. Approved drafts go to `data/approved_drafts/`.
Log every decision to `logs/sessions/<timestamp>.jsonl`. **Nothing is sent
automatically — ever.**

### Prompt 16 — IMAP read-only (`src/ingestion/imap_loader.py`)

Standard library `imaplib`. Config via env: `IMAP_HOST`, `IMAP_USER`,
`IMAP_PASS`, `IMAP_FOLDER`. Fetch unread from last N days, returning
`Email` objects with the same shape as the mbox loader. **Never** mark
read, never delete, never send. Add `--source imap` flag to `process`.
README note: app-specific password, never the main account password.

### Prompt 17 — Final eval & writeup (`scripts/generate_report.py`)

`reports/final_eval.md` with: leak rate per anonymization strategy,
utility scores per strategy, escalation precision/recall on 30 hand-labeled
emails, Pareto frontier plot (matplotlib PNG — add to requirements),
Claude API cost per email, latency breakdown by stage, and a "failure
cases" section with 5 examples where the system did the wrong thing.
Reads from real logs in `logs/`.

## Things to ask before assuming

- "Should this be a new module or extend an existing one?" — extend when the
  surface is already similar (e.g. `CombinedAnonymizer` lives next to
  `NERAnonymizer`).
- "What model string should I use for Claude?" — look it up, don't guess.
- "Should I add tests?" — yes, and prefer reusing `sensitive_spans.jsonl`
  for anything that touches anonymization.
- "Can I add a dependency?" — check it builds on Python 3.12 / Apple Silicon
  first. The package-age guard will block anything too new; the `fastcoref`
  saga is in this file as a cautionary tale.
