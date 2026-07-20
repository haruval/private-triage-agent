# private-triage-agent

A privacy-preserving email triage agent. A local model (`gemma3:27b` via
Ollama) handles your whole inbox on your machine. When an email needs harder
reasoning, it's anonymized (regex → transformer NER → neural coreference),
sent to Claude as placeholders, and re-hydrated locally. Nothing is ever sent
without your approval.

<img src="/assets/images/home.png" width="800">
<img src="/assets/images/home2.png" width="800">


## what it does

The local model triages every email: category, summary, action items, a reply
draft. Anything uncertain or sensitive-looking (legal language, negotiations,
dollar figures) gets **anonymized**, sent to Claude for a stronger draft, then
**re-hydrated** locally — Claude only ever sees placeholder text. You review
every draft first, in a React web app backed by a local API in front of the
Python pipeline.

## the anonymization stack

Three layers run in sequence, each catching what the others miss. The default
(`combined`) runs all three; `--anonymizer regex+ner` drops coref, and
`--anonymizer regex` runs the first layer alone.

1. **Deterministic regex** for fixed-shape PII: email addresses, phone
   numbers, dollar amounts, dates. Fast and exact, but won't catch anything
   without a predictable pattern.
2. **Transformer NER.** A spaCy RoBERTa pipeline (`en_core_web_trf`) runs on
   the regex-cleaned text and tags the open-ended proper nouns: people
   (`PERSON`), organizations (`ORG`), locations (`GPE`), facilities (`FAC`).
   This is what turns `Sarah` into `Alex_P1` and `Northwind` into `Acme_O1`.
3. **Neural coreference.** fastcoref (`biu-nlp/f-coref`) predicts mention
   clusters — every span that refers to the same entity, returned as character
   offsets — so "Sarah," "she," and "her" come back as one chain. Each pronoun
   gets a grammatical placeholder tied by suffix to the entity NER already
   tagged (`Sarah → Alex_P1`, `she → They_P1`, `her → Their_P1`). Claude can
   see all three refer to entity `P1`, and re-hydration restores each original
   form exactly. A bare "she" three sentences later resolves to the right
   person instead of a generic redaction. Pronouns aren't named entities, so
   NER can't touch them; only the coref chain can.
   `scripts/eval_pronoun_leak.py` counts how many pronoun leaks slip through
   with and without this layer. (spaCy's own experimental coref,
   `en_coreference_web_trf`, doesn't work on Apple Silicon rn, which is why
   fastcoref is used here.)

When layers flag overlapping spans, the longest match wins, and replacements
apply right-to-left so earlier character offsets stay valid. Every entity maps
to a stable, proper-noun-shaped placeholder (`Alex_P1`, `Acme_O1`,
`Amount_M1`), so Claude reads them as normal names rather than opaque
redactions. Its system prompt requires copying every placeholder back
verbatim, which is what makes local re-hydration exact. Coreference models are
imperfect, so a held-out eval harness reports the residual PII leak rate per
layer.


Here's one email going through the pipeline.

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

PII becomes proper-noun-shaped placeholders; the mapping stays local:
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



## layout

- `src/ingestion/` - loaders for `.mbox` files and read-only IMAP
- `src/triage/` - local model classification and drafting
- `src/anonymize/` - regex / NER / coref anonymizers, mapping store, re-hydration
- `src/router/` - sensitivity scoring, escalation logic, importance ranking
- `src/delegate/` - Claude API client
- `src/review_queue.py` - append-only processed/reviewed ledgers behind `start` + `review`
- `src/review_actions.py` - shared persistence for review decisions (CLI + web)
- `src/api/` - localhost-only HTTP API behind the web review UI
- `src/eval/` - evaluation harness and leak detector
- `frontend/` - Vite + React + Material Web review UI (talks to `src/api/`)
- `tests/` - pytest tests
- `data/` - gitignored: corpora, the `inbox/` mbox folder, `queue/` ledgers, approved drafts
- `configs/` - YAML config files

## get started

You need Python 3.12+, Node.js 20+, and [Ollama](https://ollama.com/)
installed and running, with `gemma3:27b` pulled:

```sh
ollama pull gemma3:27b    # about 17 GB
```

Then install once from the repo root:

```sh
make install              # create venv and install/cache the local models
cd frontend && npm install
cd ..
```

`make install` uses `python3.12` by default. If that exact name isn't on your
PATH, point it at your interpreter: `make install PYTHON_BIN=python3` (still
needs to be 3.12+).

The install runs a package-age check that rejects any locked dependency
published less than 14 days ago — it checks PyPI upload times, plus the GitHub
release timestamp for the hashed `en_core_web_trf` wheel, and fails closed
when metadata can't be verified. If you've reviewed a fresh dependency and
want it anyway: `ALLOW_RECENT_PACKAGES=1 make install`.

The install also downloads about 365 MB for `biu-nlp/f-coref`. That model is
locked in `configs/coref_model.lock.json` to an immutable commit, with its
official commit time, required files, and a SHA-256 for each one. The same
14-day window applies to the model revision; `ALLOW_RECENT_MODELS=1` bypasses
just the model check.

Setup uses the shared Hugging Face cache only as a download source, then
copies exactly the locked files into an isolated runtime directory under
`venv/`. The copy has no symlinks or unlisted files and is hash-checked before
every load, so Transformers can't quietly pick a different checkpoint. Mail
processing uses only that copy and an in-memory spaCy tokenizer — it never
contacts Hugging Face or downloads another model. If the runtime copy goes
missing, `venv/bin/python scripts/cache_coref_model.py` restores it.
`make clean` removes it; the shared download cache can stay so the next
install reuses it.

Claude escalation and ranking need `ANTHROPIC_API_KEY`:

```sh
cp .env.example .env      # fill in ANTHROPIC_API_KEY
```

The key is optional: without it, drafts stay local and the queue sorts by
escalation score instead. IMAP settings can be entered through the web UI's
**Connect IMAP** form, which writes them to `.env`.

After that one-time setup, start the whole app with:

```sh
python triage
```

This starts the API and Vite server in the right order, opens
`http://localhost:5173` in your browser, and stops both when you press Ctrl-C.
Use `python triage --no-browser` if you don't want the browser opened for you.
No need to activate the venv — the launcher uses `venv/` and expects
`frontend/node_modules/` from the setup above.

### 1. run

Start the app with `python triage` as above. One note: the API writes a
per-run token to `frontend/.dev-token`, and the Vite proxy injects it into
every `/api` request — browser JavaScript never sees it.

<img src="/assets/images/empty.png" width="800">

### 2. add mail

Click **Connect IMAP**, enter an app-specific password, and choose **Save &
process mail**. The form saves the `IMAP_*` values to `.env`, verifies the
Inbox and Drafts folders, and fetches unread mail without marking it read.

Or click **Upload .mbox** to pick an exported mailbox. The app copies it into
`data/inbox/` and starts processing new messages. This currently only works on
Mac; on other platforms you'll have to drag the file in yourself. Sorry!

<img src="/assets/images/imap2.png" width="800">


### 3. review drafts

Pick an email from the ranked queue to see the original message, summary,
action items, escalation decision, and an editable draft.

<img src="/assets/images/queue.png" width="800">
<img src="/assets/images/details.png" width="800">

Choose **Approve**, **Approve edit**, or **Reject**. The reviewed email leaves
the pending queue and the next one is selected. Approved drafts land in
`data/approved_drafts/` and, depending on where the email came from, also
become a click-to-open `.eml` or an IMAP draft — see
[sending approved replies](#sending-approved-replies). Every decision is
logged under `logs/sessions/`.

<img src="/assets/images/draft.png" width="800">

State lives in two append-only ledgers under `data/queue/`
(`processed.jsonl`, `reviewed.jsonl`), so processing again only adds unseen
mail, and reviewed email never reappears. If Claude is unreachable, drafts
stay local and the queue sorts by escalation score instead.

## email ingestion

### Method 1: connect your inbox over IMAP

Use **Connect IMAP** in the web UI to save the account settings and fetch
unread mail. The equivalent terminal commands are in the
[CLI section](#cli-optional) below.

Reading is **read-only** (stdlib `imaplib`): the folder opens with
`readonly=True` and bodies fetch with `BODY.PEEK[]`, so nothing gets marked
read, deleted, or sent. The one write the IMAP layer ever makes is saving an
approved reply into your **Drafts** folder (see
[sending approved replies](#sending-approved-replies)) — that APPEND is
append-only and still never sends, marks read, or deletes. Configure via
environment variables:

```
IMAP_HOST=imap.gmail.com
IMAP_USER=you@example.com
IMAP_PASS=<imap app password *see below*>
IMAP_FOLDER=INBOX          # optional
IMAP_DRAFTS_FOLDER=[Gmail]/Drafts  # Gmail; provider is prefilled in the web UI
```

**USE A PASSWORD JUST FOR THIS, NOT YOUR REAL ACCOUNT PASSWORD. I WOULD NOT TRUST ME THAT MUCH.** For Gmail
that's Google Account → Security → 2-Step Verification → App passwords; most
providers have an equivalent.

### Method 2: upload your emails as an .mbox

On a Mac, Apple Mail is the easiest way to export directly to `.mbox`:

1. Open Apple Mail.
2. Go to Mailbox > New Mailbox in the top menu bar and create a local folder (e.g., name it "Weekly Export" and set the location to "On My Mac").
3. Use the search bar to find your week. You can use search operators like date:06/02/2026-06/09/2026.
4. Select all the emails in the search results (Cmd + A) and drag them into your new "Weekly Export" mailbox.
5. Right-click the "Weekly Export" mailbox in your sidebar and select Export Mailbox.
6. Upload the file with the **Upload .mbox** button in the web UI, or drag it into `data/inbox/` and run the CLI command.

## sending approved replies

The pipeline never sends mail. Approving a draft just persists it so you can
send it yourself; where it goes depends on how the email came in.

1. **Plain text (always).** Every approved draft is written to
   `data/approved_drafts/<message-id>.txt`.
2. **IMAP source goes to Drafts.** When the email came in over IMAP
   (`start-imap`), the reply is APPENDed straight into your account's
   **Drafts** folder, flagged as a draft, so it shows up in Gmail / Apple
   Mail / Outlook ready to review and send — in the same client the message
   came from.
3. **mbox source creates a `.eml`.** When the email came from an `.mbox`
   file, an `.eml` is written next to the `.txt`. Double-clicking it opens a
   fully pre-filled reply in your email client, one click from sending.

## security

Localhost is not a security boundary — any web page open in your browser can
try to reach a local port. So every API request needs a per-run token, which
the Vite proxy injects so browser JavaScript never sees it, plus strict `Host`
and `Origin` checks. The browser never receives the anonymization mapping or
the IMAP password.

All processing of raw sensitive content runs on your machine: triage,
sensitivity scoring, anonymization, and re-hydration are all local. The only
text that ever leaves is the anonymized version sent to Claude on escalation.
The IMAP connection is read-only, enforced twice:

```python
status, _ = client.select(folder, readonly=True)  # server rejects flag changes
...
status, fetch_data = client.uid("fetch", uid, "(BODY.PEEK[])")  # never sets \Seen
```

So nothing is ever marked read, deleted, or sent; the one write is the
explicit APPEND of an approved reply into your Drafts folder.

The supply chain is locked down too. Python dependencies install from a
lockfile with exact pins, and the spaCy NER wheel is pinned to its SHA-256.
`make install` runs a package-age check that rejects anything published less
than 14 days ago and fails closed when metadata can't be verified, which
blocks freshly published (and possibly compromised) releases. The coref model
is locked to an immutable commit in `configs/coref_model.lock.json` with a
SHA-256 for every required file; setup copies exactly those files into an
isolated runtime directory that is hash-checked before every load, and mail
processing never downloads models or contacts Hugging Face.

## CLI (optional)

```sh
source venv/bin/activate
python -m src.cli start [folder]       # process new mbox mail (default data/inbox)
python -m src.cli start-imap --days 7  # same, from unread IMAP mail
python -m src.cli review               # approve / edit / reject, most important first
python -m src.cli reset [-y]           # clear the queue ledgers; -y skips the prompt
```

`start`/`start-imap` also take `--limit`, `--anonymizer regex|regex+ner|combined`,
`--task`, `--config`, and `--queue-dir`; `review` also takes `--queue-dir`,
`--approved-dir`, `--sessions-dir`, and `--max-chars`. `reset` deletes the
processed/reviewed ledgers so the next `start` reprocesses everything —
approved drafts and session logs are kept.

## development testing stuff

The commands below run against the Enron dev corpus (or any `.mbox` you point
them at) rather than your own mail. They let you inspect individual pipeline
stages and run the tests.

### build the test inbox (50 emails from the Enron dataset)

First fetch the dev corpus. This streams the ~423 MB CMU Enron tarball
(cached under `data/raw/`) and samples it down to `data/dev_corpus.mbox`:

```sh
python scripts/fetch_enron.py
```

Then sample 50 messages from it into the test inbox:

```sh
python - <<'EOF'
import mailbox, random, os
os.makedirs('data/inbox', exist_ok=True)
msgs = list(mailbox.mbox('data/dev_corpus.mbox'))
out = mailbox.mbox('data/inbox/enron_50.mbox')
for m in random.Random(42).sample(msgs, 50): out.add(m)
out.flush(); out.close()
EOF
```

### process (single command, no queue)

`process` is the single-command version of the pipeline: triage, escalate, and
review in one sitting, nothing persisted between runs. For each email: triage
locally, score sensitivity, and if it escalates, anonymize, send to Claude,
and rehydrate the reply. Every email is shown with its classification,
escalation decision, and draft (tagged `local` or `Claude`); you then approve
/ edit / reject. Approved drafts and session logs land in the same places as
`review`. **Nothing is ever sent automatically.** Escalations need
`ANTHROPIC_API_KEY` (from `.env`); a run with nothing to escalate never calls
Claude.

Processing runs on a background thread: while you review the first email, the
rest of the batch is already being triaged and delegated, so each review
starts as soon as that email is ready. `process-old` is the original fully
sequential version (process one, review one, repeat) with the same flags and
output.

```sh
source venv/bin/activate

# Interactive review of the first 10
python -m src.cli process data/dev_corpus.mbox --limit 10

# Sequential version (no background processing)
python -m src.cli process-old data/dev_corpus.mbox --limit 10

# Present + log only, no approve/reject prompts (good for a quick look or CI)
python -m src.cli process data/dev_corpus.mbox --limit 3 --no-input

# Pick the anonymizer used for escalations (default: combined = regex + NER + coref)
python -m src.cli process data/dev_corpus.mbox --limit 5 --anonymizer regex

# Reproducible random sample
python -m src.cli process data/dev_corpus.mbox --limit 5 --shuffle --seed 42

# Read unread mail over IMAP instead of an mbox file (same env vars as start-imap)
python -m src.cli process --source imap --days 7 --limit 10
```

Other flags: `--task` (the instruction sent to Claude), `--config` (router YAML,
default `configs/router.yaml`), `--approved-dir`, `--sessions-dir`, `--max-chars`
(truncate the displayed original).

### inspect the triage stage

```sh
source venv/bin/activate

# Deterministic, first 5
python -m src.cli triage-emails data/dev_corpus.mbox --limit 5

# Different random 5 each run
python -m src.cli triage-emails data/dev_corpus.mbox --limit 5 --shuffle

# Same random 5 every run (reproducible)
python -m src.cli triage-emails data/dev_corpus.mbox --limit 5 --shuffle --seed 42
```

### preview the anonymizer

Shows exactly what would leave the box on escalation; `--anonymizer` picks how
much of the stack runs:

```sh
python -m src.cli anonymize-emails data/dev_corpus.mbox --limit 2
python -m src.cli anonymize-emails data/dev_corpus.mbox --anonymizer regex --limit 2
python -m src.cli anonymize-emails data/dev_corpus.mbox --anonymizer regex+ner --shuffle --seed 42
```

### run the test suite

```sh
make test    # run the test suite
make clean   # remove venv and caches
```

Or drive pytest directly:

```sh
source venv/bin/activate

python -m pytest                       # all tests (live Claude tests need ANTHROPIC_API_KEY)
python -m pytest -m integration        # only the live Claude API integration tests
python -m pytest -m "not integration"  # everything offline, no key needed
```

### utility eval (does anonymization preserve enough meaning for Claude?)

Runs ~10 escalate-worthy emails through the raw / regex / full pipelines and
scores the drafts with `gemma3:27b` as judge:

```sh
python -m src.eval.utility_eval                                # default
python -m src.eval.utility_eval --num-emails 3 --scan-limit 20 # quick run
```
