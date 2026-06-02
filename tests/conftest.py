"""Shared pytest fixtures and configuration.

Loads the repo-root .env so real API tests can read ANTHROPIC_API_KEY, and
registers the ``integration`` marker used by tests that hit the live Claude
API. Integration tests run by default (the project policy is to exercise the
real API); use ``pytest -m "not integration"`` to skip them, and they
self-skip when no API key is available.
"""

from __future__ import annotations

import os

import pytest

from src.config import load_env_file

# Make .env values available to the whole test session.
load_env_file()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: test hits the live Claude API (needs ANTHROPIC_API_KEY)",
    )


def has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


requires_anthropic = pytest.mark.skipif(
    not has_anthropic_key(),
    reason="ANTHROPIC_API_KEY not set (in env or .env)",
)
