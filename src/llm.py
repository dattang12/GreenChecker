"""Anthropic client construction and cost accounting.

One place that knows about credentials, model ids, and prices, so nothing else
has to. Prices are per million tokens, from the published rate card.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

import anthropic
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Fix loop and LLM judge.
MODEL_FIX = "claude-sonnet-5"
# Phase 7's cheap triage call.
MODEL_TRIAGE = "claude-haiku-4-5"

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # model: (input, output)
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}

# Cache reads bill at 10% of the input rate; cache writes at 125%.
_CACHE_READ_MULTIPLIER = 0.10
_CACHE_WRITE_MULTIPLIER = 1.25


def load_env() -> None:
    load_dotenv(ROOT / ".env")


def make_client() -> anthropic.Anthropic:
    """Build a client, including the workspace header identity-linked keys need.

    An identity-linked API key is rejected with a 400 unless every request
    carries `anthropic-workspace-id`. The SDK has no constructor argument for
    it, so it goes in as a default header.
    """
    load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (expected in .env)")

    headers = {}
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if workspace:
        headers["anthropic-workspace-id"] = workspace

    return anthropic.Anthropic(api_key=key, default_headers=headers or None)


@dataclass
class Usage:
    """Running token and cost total across every call in one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    per_model: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, usage: object) -> float:
        """Fold one response's usage in; return the cost of that call alone."""
        rate_in, rate_out = PRICES_PER_MTOK.get(model, (0.0, 0.0))

        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cread = getattr(usage, "cache_read_input_tokens", 0) or 0
        cwrite = getattr(usage, "cache_creation_input_tokens", 0) or 0

        cost = (
            inp * rate_in
            + out * rate_out
            + cread * rate_in * _CACHE_READ_MULTIPLIER
            + cwrite * rate_in * _CACHE_WRITE_MULTIPLIER
        ) / 1_000_000

        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cread
        self.cache_write_tokens += cwrite
        self.cost_usd += cost
        self.calls += 1
        self.per_model[model] = self.per_model.get(model, 0) + 1
        return cost

    def as_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "llm_calls": self.calls,
        }
