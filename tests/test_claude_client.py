"""Tests for src/delegate/claude_client.py.

Per project policy these exercise the REAL Claude API (key loaded from .env by
conftest). The behavioral tests are marked ``integration`` and self-skip if no
key is present.

The only place a fake client is injected is the retry/backoff tests: the live
API can't be made to rate-limit or drop a connection on demand, so error-path
handling is verified with deterministic fault injection. The pure helpers
(prompt construction, text extraction) need no network at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anthropic
import httpx
import pytest

from src.delegate import claude_client
from src.delegate.claude_client import (
    SYSTEM_PROMPT,
    ClaudeClient,
    _build_user_prompt,
    _extract_text,
    delegate,
)

from tests.conftest import requires_anthropic

# Keep live calls cheap/fast.
_SMALL_TOKENS = 256


# ---------------------------------------------------------------------------
# Pure helpers (no network)
# ---------------------------------------------------------------------------


def test_system_prompt_demands_placeholder_preservation() -> None:
    # Guard against accidentally weakening the contract the rehydrator relies on.
    assert "Alex_P1" in SYSTEM_PROMPT
    assert "EXACTLY" in SYSTEM_PROMPT or "exactly" in SYSTEM_PROMPT


def test_system_prompt_explains_linked_pronoun_placeholders() -> None:
    assert "They_P1" in SYSTEM_PROMPT
    assert "Their_P1" in SYSTEM_PROMPT
    assert "same entity" in SYSTEM_PROMPT


def test_user_prompt_includes_task_thread_and_email() -> None:
    prompt = _build_user_prompt("the email", "the thread", "Do the thing")
    assert "Task: Do the thing" in prompt
    assert "the thread" in prompt
    assert "the email" in prompt
    assert prompt.index("the thread") < prompt.index("the email")


def test_user_prompt_omits_thread_when_absent() -> None:
    prompt = _build_user_prompt("the email", None, "Do the thing")
    assert "Earlier in the thread" not in prompt
    assert "the email" in prompt


def test_extract_text_concatenates_blocks() -> None:
    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    class _R:
        content = [_Block("foo "), _Block("bar")]

    assert _extract_text(_R()) == "foo bar"


def test_extract_text_handles_empty_content() -> None:
    class _R:
        content = None

    assert _extract_text(_R()) == ""


# ---------------------------------------------------------------------------
# Live API behavior
# ---------------------------------------------------------------------------


@requires_anthropic
@pytest.mark.integration
def test_delegate_returns_text_and_writes_log(tmp_path: Path) -> None:
    log = tmp_path / "claude.jsonl"
    client = ClaudeClient(log_path=log, max_tokens=_SMALL_TOKENS)

    out = client.delegate(
        "Subject: Lunch\nFrom: jordan@example.com\n\n"
        "Are you free for lunch Wednesday or Thursday?",
        task="Draft a one-line reply.",
    )

    assert isinstance(out, str) and out.strip()

    record = json.loads(log.read_text().strip())
    assert record["model"] == client.model
    assert record["input_tokens"] and record["input_tokens"] > 0
    assert record["output_tokens"] and record["output_tokens"] > 0
    assert record["output_length"] == len(out)
    assert isinstance(record["latency_ms"], int)
    assert "timestamp" in record


@requires_anthropic
@pytest.mark.integration
def test_delegate_preserves_placeholders(tmp_path: Path) -> None:
    """Claude should echo placeholder tokens verbatim, per the system prompt."""
    client = ClaudeClient(log_path=tmp_path / "c.jsonl", max_tokens=_SMALL_TOKENS)
    out = client.delegate(
        "Subject: Meeting\nFrom: Alex_P1\n\n"
        "Hi, can you confirm the Acme_O1 meeting on Date_D1?",
        task="Draft a short reply that addresses the sender by their token name.",
    )
    # The contract that rehydration depends on: placeholders come back intact.
    assert "Alex_P1" in out


@requires_anthropic
@pytest.mark.integration
def test_module_level_delegate(tmp_path: Path) -> None:
    client = ClaudeClient(log_path=tmp_path / "c.jsonl", max_tokens=_SMALL_TOKENS)
    out = delegate("Subject: Hi\nFrom: a@b.com\n\nPing.", None, "Reply in one word.", client=client)
    assert isinstance(out, str) and out.strip()


# ---------------------------------------------------------------------------
# Model resolution (no live call — construction only)
# ---------------------------------------------------------------------------


@requires_anthropic
def test_model_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_MODEL", "claude-from-env")
    client = ClaudeClient(log_path=tmp_path / "c.jsonl")
    assert client.model == "claude-from-env"


@requires_anthropic
def test_explicit_model_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_MODEL", "claude-from-env")
    client = ClaudeClient(model="claude-explicit", log_path=tmp_path / "c.jsonl")
    assert client.model == "claude-explicit"


# ---------------------------------------------------------------------------
# Retry / backoff — fault injection (the live API can't fail on demand)
# ---------------------------------------------------------------------------


class _RaisingMessages:
    def __init__(self, behavior: Any) -> None:
        self._behavior = behavior
        self.calls = 0

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self._behavior(self.calls)


class _FaultClient:
    def __init__(self, behavior: Any) -> None:
        self.messages = _RaisingMessages(behavior)


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]
        self.usage = None


def test_retries_transient_error_then_succeeds(tmp_path: Path) -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def behavior(call_num: int) -> Any:
        if call_num == 1:
            raise anthropic.APIConnectionError(message="boom", request=req)
        return _Response("recovered")

    fault = _FaultClient(behavior)
    client = ClaudeClient(log_path=tmp_path / "c.jsonl", client=fault, max_retries=3)

    with patch.object(claude_client.time, "sleep") as mock_sleep:
        out = client.delegate("anonymized email")

    assert out == "recovered"
    assert fault.messages.calls == 2
    assert mock_sleep.call_count == 1


def test_gives_up_after_max_retries(tmp_path: Path) -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def behavior(call_num: int) -> Any:
        raise anthropic.APIConnectionError(message="still down", request=req)

    fault = _FaultClient(behavior)
    client = ClaudeClient(log_path=tmp_path / "c.jsonl", client=fault, max_retries=2)

    with patch.object(claude_client.time, "sleep"):
        with pytest.raises(anthropic.APIConnectionError):
            client.delegate("anonymized email")

    assert fault.messages.calls == 3  # initial + 2 retries
    assert len((tmp_path / "c.jsonl").read_text().strip().splitlines()) == 1


def test_backoff_seconds_grows_and_is_capped() -> None:
    # Pure function: full-jitter backoff stays within [0, capped ceiling].
    for attempt in range(1, 8):
        s = claude_client._backoff_seconds(attempt)
        assert 0.0 <= s <= claude_client.BACKOFF_MAX_SECONDS
