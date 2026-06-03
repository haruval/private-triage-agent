# private-triage-agent

A privacy-preserving email triage agent. A local model (`gemma3:27b` via Ollama) handles most processing; sensitive content is anonymized before being sent to the Claude API for harder reasoning, then re-hydrated locally.

## What it does

The local model triages every email: category, summary, action items, a reply
draft. When it's uncertain or the content looks sensitive (legal, negotiation,
dollar figures), the email is **anonymized**, sent to Claude for a stronger
draft, then **re-hydrated** locally. Nothing is sent automatically: every draft
is reviewed by you first. Here's one example of an email going through the pipeline.

**1. Incoming email**

```
Subject: Contract renewal - need your sign-off by Friday
From: sarah.chen@northwind.com

Following up on the Northwind service contract. Legal flagged two changes to the
liability cap and we should push back before signing. Can you review the redlines
and confirm the $250,000 figure before Friday?

Also - can we move our call to Thursday? Reach me at (415) 555-0182.
```

**2. Local triage** (`gemma3:27b`) - never leaves your machine

```
category    : action_required   (confidence 0.85)
action items:
  - Review the contract redlines
  - Confirm the $250,000 figure
  - Reschedule the call
```

The legal redlines and the dollar figure make this a candidate for escalation.

**3. Anonymized before delegation** - this is all Claude sees

```
Subject: Contract renewal - need your sign-off by Date_D1
From: Email_E1

Following up on the Acme_O1 service contract. Legal flagged two changes to the
liability cap and we should push back before signing. Can you review the redlines
and confirm the Amount_M1 figure before Date_D1?

Also - can we move our call to Date_D2? Reach me at Phone_F1.
```

PII becomes proper-noun-shaped placeholders; the mapping stays local —
`Email_E1 → sarah.chen@northwind.com`, `Acme_O1 → Northwind`,
`Amount_M1 → $250,000`, `Phone_F1 → (415) 555-0182`, `Date_D1 → Friday`,
`Date_D2 → Thursday`, `Alex_P1 → Sarah`.

**4. Claude's draft, re-hydrated locally** - placeholders swapped back, ready for review

```
Hi Sarah,

Thanks for the heads up. I'll review the redlines and liability cap changes today
and get back to you on the $250,000 figure by end of business tomorrow.

Thursday works for my schedule. I'll give you a call at (415) 555-0182 to confirm
timing.
```

## Layout

- `src/ingestion/` - loaders for `.mbox` files and (later) IMAP
- `src/triage/` - local model classification and drafting
- `src/anonymize/` - anonymization, mapping store, re-hydration
- `src/router/` - sensitivity scoring and escalation logic
- `src/delegate/` - Claude API client
- `src/eval/` - evaluation harness and leak detector
- `tests/` - pytest tests
- `data/` - gitignored, for corpora
- `configs/` - YAML config files

## Setup

Requires Python 3 and [Ollama](https://ollama.com/) installed locally with `gemma3:27b` pulled.

```sh
make install            # create venv, install requirements, download spaCy model
ALLOW_RECENT_PACKAGES=1 make install            #i put a min package date lock to be extra safe but you can bypass it with this
cp .env.example .env    # fill in ANTHROPIC_API_KEY
```

## Usage

```sh
make test    # run the test suite
make clean   # remove venv and caches
```

## test triage cli
source venv/bin/activate
### Default behavior (deterministic, first 5)
python -m src.cli triage-emails data/dev_corpus.mbox --limit 5

### Different random 5 each run
python -m src.cli triage-emails data/dev_corpus.mbox --limit 5 --shuffle

### Same random 5 every run (reproducible)
python -m src.cli triage-emails data/dev_corpus.mbox --limit 5 --shuffle --seed 42

## test anonmymizer
python -m src.cli anonymize-emails data/dev_corpus.mbox --limit 2
python -m src.cli anonymize-emails data/dev_corpus.mbox --anonymizer regex --limit 2
python -m src.cli anonymize-emails data/dev_corpus.mbox --anonymizer coref --shuffle --seed 42

## process emails (the full pipeline)
source venv/bin/activate
For each email: triage locally → score sensitivity → if it escalates, anonymize,
send to Claude, and rehydrate the reply. Every email is shown with its
classification, escalation decision, and draft (tagged `local` or `Claude`); you
then approve / edit / reject. Approved drafts are written to
`data/approved_drafts/` and every decision is logged to
`logs/sessions/<timestamp>.jsonl`. **Nothing is ever sent automatically.**
Escalations need `ANTHROPIC_API_KEY` (from `.env`); a run with nothing to
escalate never calls Claude.

### Interactive review of the first 10
python -m src.cli process data/dev_corpus.mbox --limit 10

### Present + log only, no approve/reject prompts (good for a quick look or CI)
python -m src.cli process data/dev_corpus.mbox --limit 3 --no-input

### Pick the anonymizer used for escalations (default: combined = regex + NER)
python -m src.cli process data/dev_corpus.mbox --limit 5 --anonymizer regex

### Reproducible random sample
python -m src.cli process data/dev_corpus.mbox --limit 5 --shuffle --seed 42

Other flags: `--task` (the instruction sent to Claude), `--config` (router YAML,
default `configs/router.yaml`), `--approved-dir`, `--sessions-dir`, `--max-chars`
(truncate the displayed original).

## run the test suite
source venv/bin/activate
### All tests (includes the live Claude API integration tests; needs ANTHROPIC_API_KEY)
python -m pytest

### Live Claude API integration tests only
python -m pytest -m integration

### Everything except the live Claude API tests (offline, no key needed)
python -m pytest -m "not integration"

## test utility eval (does anonymization preserve enough meaning for Claude?)
### Default: 10 escalate-worthy emails through the raw / regex / full pipelines, judged by gemma3:27b
python -m src.eval.utility_eval

### Quick run: fewer emails, bounded mbox scan
python -m src.eval.utility_eval --num-emails 3 --scan-limit 20