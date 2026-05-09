"""Thin wrapper around the ollama Python library for the local triage model.

Defaults to gemma3:27b (override via OLLAMA_MODEL env var). Logs every call as
JSONL to logs/ollama_calls.jsonl for latency and cost analysis.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ollama

DEFAULT_MODEL = "gemma3:27b"
DEFAULT_LOG_PATH = Path("logs/ollama_calls.jsonl")
DEFAULT_TEMPERATURE = 0.2
JSON_MAX_ATTEMPTS = 3

# Greedy match from first '{' to last '}' across newlines. Imperfect for
# pathological inputs (multiple JSON objects, braces inside strings of prose),
# but a solid first pass when the model wraps JSON in explanation or code fences.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class JSONExtractionError(ValueError):
    """Raised when the model's output can't be parsed as JSON after all retries."""


class OllamaClient:
    """Synchronous client targeting a single local Ollama model.

    Cheap to construct — no warm-up. The first call will block while Ollama
    loads the model into memory (can be 10–30s for gemma3:27b).
    """

    def __init__(
        self,
        model: str | None = None,
        log_path: Path | str | None = None,
    ) -> None:
        self.model = model or os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        """Run a chat call and return the model's text reply."""
        start = time.perf_counter()
        text = ""
        try:
            text = self._chat(prompt, system, temperature)
            return text
        finally:
            self._log_call(
                prompt_length=len(prompt),
                latency_ms=int((time.perf_counter() - start) * 1000),
                output_length=len(text),
            )

    def generate_json(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> dict[str, Any]:
        """Run a chat call and parse the response as JSON.

        Tries each response twice — first as raw JSON, then as a regex-extracted
        ``{...}`` block — and re-prompts up to JSON_MAX_ATTEMPTS times if both
        attempts fail. Raises JSONExtractionError if all retries are exhausted.
        """
        start = time.perf_counter()
        text = ""
        last_error: Exception | None = None
        try:
            for _ in range(JSON_MAX_ATTEMPTS):
                text = self._chat(prompt, system, temperature)
                parsed = _try_parse_json(text)
                if parsed is not None:
                    return parsed
                last_error = parsed_error_for(text)
            raise JSONExtractionError(
                f"Could not parse JSON after {JSON_MAX_ATTEMPTS} attempts. "
                f"Last error: {last_error}. Last response: {text[:200]!r}"
            )
        finally:
            self._log_call(
                prompt_length=len(prompt),
                latency_ms=int((time.perf_counter() - start) * 1000),
                output_length=len(text),
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _chat(
        self,
        prompt: str,
        system: str | None,
        temperature: float,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = ollama.chat(
            model=self.model,
            messages=messages,
            options={"temperature": temperature},
        )
        return response["message"]["content"]

    def _log_call(
        self,
        prompt_length: int,
        latency_ms: int,
        output_length: int,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt_length": prompt_length,
            "latency_ms": latency_ms,
            "output_length": output_length,
            "model": self.model,
        }
        with self.log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Module-level helpers (kept out of the class for trivial reuse / testing)
# ---------------------------------------------------------------------------


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """Try parsing `text` as JSON, then try a regex-extracted {...} block.

    Returns the parsed dict on success, or None if both attempts fail.
    Returns None for non-dict JSON (e.g. a top-level list) — the public API
    contract is dict, not arbitrary JSON.
    """
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None

    if not isinstance(value, dict):
        match = _JSON_BLOCK.search(text)
        if match:
            try:
                value = json.loads(match.group(0))
            except json.JSONDecodeError:
                value = None

    return value if isinstance(value, dict) else None


def parsed_error_for(text: str) -> Exception | None:
    """Surface a representative parse error for diagnostic logging."""
    try:
        json.loads(text)
        return None
    except json.JSONDecodeError as e:
        return e
