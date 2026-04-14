"""Chatbot package — app onboarding + feature recommendation chatbot.

Public entry point: ChatbotSession (WebSocket handler).
Knowledge base: data/features/manifest.json (single source of truth).
"""

from src.chatbot.session import ChatbotSession

__all__ = ["ChatbotSession"]
