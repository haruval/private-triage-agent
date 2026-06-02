"""Claude API delegation client.

When the local model is uncertain and the router escalates, the *anonymized*
email is sent here for harder reasoning. This wrapper mirrors
:class:`OllamaClient`'s discipline: every call writes one JSON line to
``logs/claude_calls.jsonl`` with token counts and latency.

Two things are load-bearing:

- The system prompt instructs Claude to preserve ``Name_P1``-style
  placeholders verbatim, so the response can be rehydrated locally. If Claude
  rewrites or expands a placeholder, rehydration can't put the real value
  back.
- Rate-limit, transient server, and connection errors are retried with
  exponential backoff. Everything else propagates immediately.

Model string is ``claude-sonnet-4-5`` by default — verified against the
``Model`` type shipped with ``anthropic`` 0.100.0, not guessed from memory.
Override with the ``CLAUDE_MODEL`` env var or the ``model`` constructor arg.
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_LOG_PATH = Path("logs/claude_calls.jsonl")
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.3
DEFAULT_TASK = "Draft a concise, professional reply to the email below."

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0


SYSTEM_PROMPT = """\
You are an email assistant working on ANONYMIZED email.

Some proper nouns have been replaced with placeholder tokens that read like
ordinary names:

    Alex_P1     a person          Acme_O1     an organization
    Westview_G1 a place           Email_E1    an email address
    Phone_F1    a phone number    Amount_M1   a dollar amount
    Date_D1     a date            Address_A1  a street address

Treat each placeholder as the real entity it stands for. Critical rules:

- Preserve every placeholder EXACTLY as written — same prefix, underscore,
  letter, and number (e.g. always "Alex_P1", never "Alex", "Alex P1", or
  "Alex_P2").
- Do NOT invent new placeholders, and do NOT guess or expand the real value
  behind one.
- Otherwise write naturally. The placeholders are substituted back to their
  real values locally after you respond, so the recipient never sees them.
"""


class ClaudeClient:
    """Synchronous Claude client for escalated triage.

    Pass an explicit ``client`` (any object exposing ``messages.create``) for
    tests so no network call or API key is required. Otherwise an
    ``anthropic.Anthropic`` is constructed lazily, reading ``ANTHROPIC_API_KEY``
    from the environment.
    """

    def __init__(
        self,
        model: str | None = None,
        log_path: Path | str | None = None,
        *,
        client: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.model = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        if client is not None:
            self._client = client
        else:
            # Surface ANTHROPIC_API_KEY from .env if the shell didn't export it,
            # so the real client works without callers remembering to load it.
            # Treat an empty value as unset — some shells export the key as ""
            # which would otherwise mask the real value in .env.
            if not os.environ.get("ANTHROPIC_API_KEY"):
                from src.config import load_env_file
                load_env_file()
            import anthropic  # lazy: keeps the dep out of the import path for tests
            self._client = anthropic.Anthropic()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def delegate(
        self,
        anonymized_email: str,
        anonymized_thread: str | None = None,
        task: str = DEFAULT_TASK,
    ) -> str:
        """Send an anonymized email (and optional thread) to Claude.

        Returns Claude's text response — still anonymized; the caller is
        responsible for rehydrating placeholders.
        """
        user_prompt = _build_user_prompt(anonymized_email, anonymized_thread, task)
        start = time.perf_counter()
        text = ""
        usage: Any = None
        try:
            text, usage = self._create(user_prompt)
            return text
        finally:
            self._log_call(
                prompt_length=len(user_prompt),
                output_length=len(text),
                latency_ms=int((time.perf_counter() - start) * 1000),
                usage=usage,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _create(self, user_prompt: str) -> tuple[str, Any]:
        """Call the messages API, retrying transient failures with backoff."""
        import anthropic

        retryable = (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        )

        attempt = 0
        while True:
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                return _extract_text(response), getattr(response, "usage", None)
            except retryable:
                attempt += 1
                if attempt > self.max_retries:
                    raise
                time.sleep(_backoff_seconds(attempt))

    def _log_call(
        self,
        prompt_length: int,
        output_length: int,
        latency_ms: int,
        usage: Any,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt_length": prompt_length,
            "output_length": output_length,
            "latency_ms": latency_ms,
            "model": self.model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        with self.log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def delegate(
    anonymized_email: str,
    anonymized_thread: str | None = None,
    task: str = DEFAULT_TASK,
    *,
    client: ClaudeClient | None = None,
) -> str:
    """Convenience wrapper matching the roadmap signature.

    Constructs a default :class:`ClaudeClient` unless one is supplied.
    """
    c = client if client is not None else ClaudeClient()
    return c.delegate(anonymized_email, anonymized_thread, task)


def _build_user_prompt(
    anonymized_email: str,
    anonymized_thread: str | None,
    task: str,
) -> str:
    parts: list[str] = []
    if task:
        parts.append(f"Task: {task}")
    if anonymized_thread:
        parts.append(f"--- Earlier in the thread ---\n{anonymized_thread}")
    parts.append(f"--- Email ---\n{anonymized_email}")
    return "\n\n".join(parts)


def _extract_text(response: Any) -> str:
    """Concatenate the text blocks of an anthropic Messages response."""
    content = getattr(response, "content", None)
    if content is None:
        return ""
    chunks: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "".join(chunks)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter, capped at BACKOFF_MAX_SECONDS."""
    ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)
