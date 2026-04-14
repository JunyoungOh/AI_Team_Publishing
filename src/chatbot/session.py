"""Chatbot WebSocket session — app onboarding + feature recommendation.

Reuses the Secretary mode's ChatEngine for streaming (Claude Code subprocess
bridge, token stream), but swaps in a chatbot-specific system prompt built
live from data/features/manifest.md on every turn.

No pre-processing or scripted classification — the AI reads the manifest and
decides single-feature vs combo recommendation on its own.

Message flow:
  Browser → {"type": "bot_message", "data": {"content": "..."}}
  Server  → {"type": "bot_stream", "data": {"token": "...", "done": false}}
  Server  → {"type": "bot_stream", "data": {"token": "", "done": true}}
"""

from __future__ import annotations

import logging
import uuid

from src.chatbot.config import ChatbotConfig
from src.chatbot.knowledge import build_system_prompt
from src.secretary.chat_engine import ChatEngine
from src.utils.claude_code import set_session_tag

logger = logging.getLogger(__name__)


class _WsEventRenamer:
    """Thin proxy that rewrites ChatEngine's hardcoded `sec_*` event types
    into `bot_*` on the fly, so the frontend can listen on its own namespace
    without touching shared ChatEngine code."""

    _RENAME = {
        "sec_stream": "bot_stream",
        "sec_turn_start": "bot_turn_start",
        "sec_turn_end": "bot_turn_end",
    }

    def __init__(self, inner):
        self._inner = inner

    async def send_json(self, payload: dict):
        t = payload.get("type")
        if t in self._RENAME:
            payload = {**payload, "type": self._RENAME[t]}
        await self._inner.send_json(payload)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ChatbotSession:
    """One WebSocket connection for the onboarding/guide chatbot.

    Unlike SecretarySession, this session:
      - has no personas, no calendar, no report generation
      - rebuilds its system prompt from manifest.md on every message
        (so edits take effect immediately, no restart needed)
      - does no pre-classification — user messages go to the LLM raw
      - uses Sonnet + medium effort (balanced semantic matching, not overkill)
    """

    def __init__(self, ws, user_id: str = ""):
        self.ws = ws
        self._cancelled = False
        self._user_id = user_id
        self._session_id = str(uuid.uuid4())[:8]
        self._session_tag = f"bot_{self._session_id}"
        self._config = ChatbotConfig()
        self._engine = ChatEngine(
            config=self._config,  # type: ignore[arg-type]
            session_tag=self._session_tag,
            session_id="",  # in-memory only, no history persistence
            user_id=user_id,
        )

    async def run(self) -> None:
        set_session_tag(self._session_tag)
        await self._send({
            "type": "bot_init",
            "data": {"status": "ready", "session_id": self._session_id},
        })

        while not self._cancelled:
            try:
                msg = await self.ws.receive_json()
            except Exception:
                break

            msg_type = msg.get("type")
            if msg_type == "bot_stop":
                break
            if msg_type == "bot_reset":
                self._engine.reset()
                await self._send({"type": "bot_reset_done", "data": {}})
                continue
            if msg_type == "bot_message":
                content = msg.get("data", {}).get("content", "").strip()
                if not content:
                    continue
                await self._handle_message(content)

    async def _handle_message(self, content: str) -> None:
        """Refresh system prompt from manifest.md, stream reply with Sonnet+medium."""
        # Rebuild from MD on every turn — manifest edits take effect immediately.
        self._engine._system_prompt_template = build_system_prompt()  # type: ignore[attr-defined]

        try:
            proxy_ws = _WsEventRenamer(self.ws)
            await self._engine.stream_response(content, proxy_ws, effort="medium")
        except Exception as e:
            logger.warning("chatbot_stream_failed: %s", e)
            await self._send({
                "type": "bot_stream",
                "data": {"token": f"\n\n⚠️ 오류: {e}", "done": True},
            })

    async def _send(self, payload: dict) -> None:
        try:
            await self.ws.send_json(payload)
        except Exception:
            pass

    def cancel(self) -> None:
        self._cancelled = True
