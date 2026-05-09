"""Local-model email triage.

Sends an Email to the local Ollama model with a system prompt containing
schema + few-shot examples, parses the structured JSON response into a
TriageResult, and validates the shape on the way through.

Confidence is asked for in the same JSON object — no second model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.ingestion.mbox_loader import Email
from src.triage.ollama_client import OllamaClient

CATEGORIES: frozenset[str] = frozenset(
    {"action_required", "fyi", "spam", "needs_reply", "unclear"}
)

DRAFT_CONFIDENCE_THRESHOLD = 0.6
MAX_BODY_CHARS = 3000


@dataclass
class TriageResult:
    category: str
    confidence: float
    summary: str
    extracted_action_items: list[str]
    suggested_reply_draft: str | None
    reasoning: str

    @classmethod
    def from_json_dict(cls, d: dict[str, Any]) -> "TriageResult":
        """Validate a parsed JSON dict and construct a TriageResult.

        Strict on category, confidence, summary, and reasoning — missing or
        wrong-typed values raise ValueError with a clear message.

        Lenient on:
          - extracted_action_items (defaults to [] if absent)
          - suggested_reply_draft (defaults to None; coerced to None when the
            category/confidence conditional isn't satisfied — the model
            sometimes drafts replies for non-needs_reply emails, and we'd
            rather drop the draft than crash)
        """
        if not isinstance(d, dict):
            raise ValueError(
                f"Expected a dict, got {type(d).__name__}"
            )

        # category — required, must be in the enum
        if "category" not in d:
            raise ValueError("Missing required field: 'category'")
        category = d["category"]
        if not isinstance(category, str):
            raise ValueError(
                f"'category' must be a string, got {type(category).__name__}"
            )
        if category not in CATEGORIES:
            raise ValueError(
                f"Invalid category {category!r}; "
                f"must be one of {sorted(CATEGORIES)}"
            )

        # confidence — required, numeric, [0, 1]
        if "confidence" not in d:
            raise ValueError("Missing required field: 'confidence'")
        confidence_raw = d["confidence"]
        if isinstance(confidence_raw, bool) or not isinstance(
            confidence_raw, (int, float)
        ):
            raise ValueError(
                f"'confidence' must be a number, got {type(confidence_raw).__name__}"
            )
        confidence = float(confidence_raw)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"'confidence' must be in [0, 1], got {confidence}"
            )

        # summary — required, non-empty string
        if "summary" not in d:
            raise ValueError("Missing required field: 'summary'")
        summary = d["summary"]
        if not isinstance(summary, str):
            raise ValueError(
                f"'summary' must be a string, got {type(summary).__name__}"
            )
        if not summary.strip():
            raise ValueError("'summary' must not be empty")

        # extracted_action_items — list of strings, default []
        items_raw = d.get("extracted_action_items", [])
        if not isinstance(items_raw, list):
            raise ValueError(
                f"'extracted_action_items' must be a list, "
                f"got {type(items_raw).__name__}"
            )
        if not all(isinstance(x, str) for x in items_raw):
            raise ValueError(
                "'extracted_action_items' must contain only strings"
            )

        # reasoning — required string
        if "reasoning" not in d:
            raise ValueError("Missing required field: 'reasoning'")
        reasoning = d["reasoning"]
        if not isinstance(reasoning, str):
            raise ValueError(
                f"'reasoning' must be a string, got {type(reasoning).__name__}"
            )

        # suggested_reply_draft — optional, conditionally enforced
        draft = d.get("suggested_reply_draft")
        if draft is not None and not isinstance(draft, str):
            raise ValueError(
                f"'suggested_reply_draft' must be a string or null, "
                f"got {type(draft).__name__}"
            )
        # Per the schema: drafts only belong on confident needs_reply results.
        # Otherwise drop them silently rather than fail validation.
        if draft is not None and (
            category != "needs_reply"
            or confidence <= DRAFT_CONFIDENCE_THRESHOLD
        ):
            draft = None

        return cls(
            category=category,
            confidence=confidence,
            summary=summary,
            extracted_action_items=list(items_raw),
            suggested_reply_draft=draft,
            reasoning=reasoning,
        )


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are an email triage assistant. You classify an email into exactly one \
category and return a structured JSON object.

Categories:
- action_required: the email asks the recipient to do a specific task \
(review, complete, attend, decide). No reply expected; just action.
- needs_reply: the email asks a question or otherwise expects a response.
- fyi: informational, no action or reply needed.
- spam: unsolicited marketing, phishing, or bulk promotional content.
- unclear: doesn't fit any category, or there isn't enough information to decide.

Respond with ONLY a JSON object — no prose, no code fences. Schema:

{
  "category": "<action_required | needs_reply | fyi | spam | unclear>",
  "confidence": <float between 0 and 1>,
  "summary": "<1-2 sentence summary>",
  "extracted_action_items": ["<action>", ...],
  "suggested_reply_draft": "<draft reply text>" | null,
  "reasoning": "<short explanation of your category choice>"
}

Rules:
- Use confidence honestly: 0.9+ when the category is obvious, 0.5-0.7 when \
plausible alternatives exist, below 0.5 only with "unclear".
- extracted_action_items: empty list when none. Each item is a concrete task \
phrased as an imperative ("Reply with availability", "Review the Q3 deck").
- suggested_reply_draft: ONLY when category is "needs_reply" AND confidence > 0.6. \
For all other categories or lower confidence, set it to null.

Examples follow.

EXAMPLE 1
INPUT:
From: manager@company.com
To: alex@company.com
Subject: Q3 deck review by Friday EOD

Hi Alex - please review the Q3 deck attached and send comments by Friday EOD. \
The CFO sees it Monday morning. Thanks.

OUTPUT:
{"category": "action_required", "confidence": 0.93, "summary": "Manager wants \
the Q3 deck reviewed with comments by Friday EOD ahead of a Monday CFO review.", \
"extracted_action_items": ["Review the Q3 deck", "Send comments by Friday EOD"], \
"suggested_reply_draft": null, "reasoning": "Direct task request with a hard \
deadline. No question to answer; the sender is delegating work."}

EXAMPLE 2
INPUT:
From: jordan@friend.example
To: ari@example.com
Subject: Lunch this week?

hey - free for lunch wednesday or thursday? trying to lock something in.

OUTPUT:
{"category": "needs_reply", "confidence": 0.88, "summary": "Jordan is asking \
whether you're free for lunch on Wednesday or Thursday this week.", \
"extracted_action_items": ["Pick Wednesday or Thursday", "Reply with availability"], \
"suggested_reply_draft": "Wednesday works for me — want to do 12:30? \
Open to wherever, your pick.", "reasoning": "Direct question requiring \
availability information back. Friendly tone; short reply expected."}

EXAMPLE 3
INPUT:
From: deals@megastore.example
To: ari@example.com
Subject: 75% OFF — TODAY ONLY!

FINAL HOURS! Don't miss our biggest sale ever. Click now to save big on \
hundreds of items. Unsubscribe at the bottom of this email.

OUTPUT:
{"category": "spam", "confidence": 0.97, "summary": "Mass-market promotional \
email about a 75%-off one-day sale.", "extracted_action_items": [], \
"suggested_reply_draft": null, "reasoning": "Bulk promotional content with \
urgency tactics and an unsubscribe link. Not personal, no action expected."}

End of examples. Now classify the next email.
"""


def _format_email_for_prompt(email: Email, max_body_chars: int = MAX_BODY_CHARS) -> str:
    body = email.body_plain or ""
    if len(body) > max_body_chars:
        body = body[:max_body_chars] + "\n[...truncated...]"
    to = ", ".join(email.to_addrs) if email.to_addrs else ""
    return (
        f"From: {email.from_addr}\n"
        f"To: {to}\n"
        f"Subject: {email.subject}\n"
        f"Date: {email.date.isoformat()}\n"
        f"\n"
        f"{body}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def triage(email: Email, client: OllamaClient | None = None) -> TriageResult:
    """Classify an email via the local Ollama model.

    Pass an explicit `client` for tests or to share an instance across calls.
    Otherwise a default OllamaClient is constructed (uses OLLAMA_MODEL env var,
    falling back to gemma3:27b).
    """
    if client is None:
        client = OllamaClient()
    user_prompt = "INPUT:\n" + _format_email_for_prompt(email) + "\n\nOUTPUT:"
    raw = client.generate_json(prompt=user_prompt, system=SYSTEM_PROMPT)
    return TriageResult.from_json_dict(raw)
