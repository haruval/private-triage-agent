# private-triage-agent

A privacy-preserving email triage agent. A local model (`gemma3:27b` via Ollama) handles most processing; sensitive content is anonymized before being sent to the Claude API for harder reasoning, then re-hydrated locally.

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
cp .env.example .env    # fill in ANTHROPIC_API_KEY
```

## Usage

```sh
make test    # run the test suite
make clean   # remove venv and caches
```

## test triage
source venv/bin/activate
python -m src.cli triage-emails data/dev_corpus.mbox --limit 5
