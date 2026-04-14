"""Chatbot mode configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatbotConfig(BaseModel):
    """Chatbot (onboarding + feature recommendation) settings.

    Model: sonnet — Haiku is too shallow for nuanced semantic matching across
      11 modes; Opus is overkill for interactive chat.
    Effort: medium — same tier as CEO routing / worker_effort in settings.py.
      High effort enables extended thinking (5–10s latency), which is too slow
      for an interactive guide chatbot. Medium gives Sonnet's reasoning without
      the thinking budget overhead.
    """

    model: str = "sonnet"
    max_history_turns: int = Field(default=12, ge=4, le=40)
    response_timeout: int = Field(default=60, ge=10, le=180)
    compress_threshold: int = Field(default=16, ge=8, le=60)
