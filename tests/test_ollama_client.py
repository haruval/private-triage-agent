"""Tests for src/triage/ollama_client.py.

The real ollama daemon and gemma3:27b are never touched — every call to
ollama.chat is patched at the module level. Each test gets its own log file
under pytest's tmp_path so logs don't pollute logs/ollama_calls.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.triage import ollama_client
from src.triage.ollama_client import JSONExtractionError, OllamaClient


def _mock_response(content: str) -> dict:
    """Shape that matches what `ollama.chat()` returns."""
    return {"message": {"content": content}}


def test_generate_returns_text_and_writes_log(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    client = OllamaClient(model="test-model", log_path=log)

    with patch.object(
        ollama_client.ollama, "chat", return_value=_mock_response("hello there")
    ) as mock_chat:
        result = client.generate("hi", system="be polite", temperature=0.5)

    assert result == "hello there"

    # Verify how we called ollama.chat
    mock_chat.assert_called_once()
    kwargs = mock_chat.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["options"]["temperature"] == 0.5
    assert kwargs["messages"] == [
        {"role": "system", "content": "be polite"},
        {"role": "user", "content": "hi"},
    ]

    # Verify log shape
    record = json.loads(log.read_text().strip())
    assert record["model"] == "test-model"
    assert record["prompt_length"] == 2
    assert record["output_length"] == 11
    assert isinstance(record["latency_ms"], int)
    assert "timestamp" in record


def test_generate_json_parses_pure_json(tmp_path: Path) -> None:
    client = OllamaClient(log_path=tmp_path / "calls.jsonl")
    with patch.object(
        ollama_client.ollama,
        "chat",
        return_value=_mock_response('{"category": "urgent", "score": 0.9}'),
    ):
        result = client.generate_json("classify this")
    assert result == {"category": "urgent", "score": 0.9}


def test_generate_json_extracts_from_prose_wrapped_response(tmp_path: Path) -> None:
    """When the model wraps JSON in explanation or fences, regex extraction kicks in."""
    client = OllamaClient(log_path=tmp_path / "calls.jsonl")
    wrapped = (
        "Sure! Here's the classification:\n\n"
        "```json\n"
        '{"category": "urgent", "score": 0.9}\n'
        "```\n"
        "Let me know if you need more!"
    )
    with patch.object(
        ollama_client.ollama, "chat", return_value=_mock_response(wrapped)
    ) as mock_chat:
        result = client.generate_json("classify")

    assert result == {"category": "urgent", "score": 0.9}
    # Should have succeeded on first call — no retries needed.
    assert mock_chat.call_count == 1


def test_generate_json_retries_until_valid(tmp_path: Path) -> None:
    """First two responses unparseable, third returns valid JSON."""
    client = OllamaClient(log_path=tmp_path / "calls.jsonl")
    responses = [
        _mock_response("not json at all"),
        _mock_response("still nope, sorry"),
        _mock_response('{"ok": true}'),
    ]
    with patch.object(
        ollama_client.ollama, "chat", side_effect=responses
    ) as mock_chat:
        result = client.generate_json("classify")

    assert result == {"ok": True}
    assert mock_chat.call_count == 3


def test_generate_json_raises_after_max_attempts(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    client = OllamaClient(log_path=log)

    with patch.object(
        ollama_client.ollama, "chat", return_value=_mock_response("never any json")
    ) as mock_chat:
        with pytest.raises(JSONExtractionError):
            client.generate_json("classify")

    # Three attempts before giving up
    assert mock_chat.call_count == 3
    # And the failed call still got logged once
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1


def test_model_picked_up_from_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "custom-model:7b")
    client = OllamaClient(log_path=tmp_path / "calls.jsonl")
    assert client.model == "custom-model:7b"

    with patch.object(
        ollama_client.ollama, "chat", return_value=_mock_response("ok")
    ) as mock_chat:
        client.generate("hi")

    assert mock_chat.call_args.kwargs["model"] == "custom-model:7b"


def test_explicit_model_overrides_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "from-env:1b")
    client = OllamaClient(model="explicit:99b", log_path=tmp_path / "calls.jsonl")
    assert client.model == "explicit:99b"
