"""Chat engine — streaming responses via Claude Code subprocess.

CLI mode: Reads stream-json stdout line-by-line from Claude Code subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

from src.secretary.config import SecretaryConfig
from src.secretary.history_store import HistoryStore
from src.secretary.prompts.system import SECRETARY_SYSTEM

logger = logging.getLogger(__name__)


class ChatMessage:
    """A single chat message."""

    __slots__ = ("role", "content", "timestamp", "message_id")

    def __init__(self, role: str, content: str, message_id: str = ""):
        self.role = role
        self.content = content
        self.timestamp = time.time()
        self.message_id = message_id


class ChatEngine:
    """Streaming chat engine via Claude Code CLI subprocess."""

    def __init__(
        self,
        config: SecretaryConfig,
        session_tag: str,
        session_id: str = "",
        user_id: str = "",
        system_prompt_template: str | None = None,
        history_store: HistoryStore | None = None,
    ):
        self.config = config
        self.session_tag = session_tag
        self.history: list[ChatMessage] = []
        self._system_prompt_template = system_prompt_template
        if history_store is not None:
            self._store = history_store
        else:
            self._store = HistoryStore(session_id, user_id=user_id) if session_id else None

    def load_history(self) -> int:
        """Load persisted history into memory. Returns number of messages restored."""
        if not self._store:
            return 0
        raw = self._store.load()
        if not raw:
            return 0
        self.history = []
        for m in raw:
            msg = ChatMessage(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                message_id=m.get("message_id", ""),
            )
            msg.timestamp = m.get("timestamp", 0)
            self.history.append(msg)
        return len(self.history)

    def reset(self, system_prompt_template: str | None = None) -> None:
        """Reset chat: clear history (memory + disk) and optionally change prompt template."""
        self.history = []
        self._persist()  # Write empty history to disk
        self._system_prompt_template = system_prompt_template

    def _persist(self) -> None:
        """Save current history to disk."""
        if not self._store:
            return
        data = [
            {
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp,
                "message_id": m.message_id,
            }
            for m in self.history
        ]
        self._store.save(data)

    def _build_user_prompt(self, message: str) -> str:
        """Build the full user prompt with conversation history inline."""
        if not self.history:
            return message

        recent = self.history[-self.config.max_history_turns:]
        parts = []
        for msg in recent:
            if msg.role == "system":
                parts.append(msg.content)
                continue
            role = "User" if msg.role == "user" else "Assistant"
            parts.append(f"[{role}] {msg.content}")
        parts.append(f"[User] {message}")
        return (
            "다음은 이전 대화 내용입니다. 마지막 User 메시지에 답변하세요.\n\n"
            + "\n\n".join(parts)
        )

    def _build_context(self) -> str:
        """Build conversation context from history for system prompt."""
        if not self.history:
            return "(새로운 대화입니다)"
        recent = self.history[-self.config.max_history_turns:]
        lines = []
        for msg in recent:
            if msg.role == "system":
                lines.append(msg.content)
                continue
            role = "User" if msg.role == "user" else "Assistant"
            lines.append(f"[{role}] {msg.content}")
        return "\n\n".join(lines)

    async def stream_response(self, user_message: str, ws, effort: str = "low") -> str:
        """Stream a response via Claude Code CLI."""
        return await self._stream_cli(user_message, ws)

    async def _stream_cli(self, user_message: str, ws) -> str:
        """Stream response via Claude Code subprocess (original implementation)."""
        from src.utils.claude_code import (
            _register_process,
            _unregister_process,
            _kill_process_tree,
            set_session_tag,
        )

        set_session_tag(self.session_tag)

        template = self._system_prompt_template or SECRETARY_SYSTEM
        system_prompt = template.format(context=self._build_context())
        user_prompt = self._build_user_prompt(user_message)

        # Uses asyncio.create_subprocess_exec (not shell) — safe from injection
        cmd = [
            "claude", "-p", user_prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", self.config.model,
            "--max-turns", "3",
            "--append-system-prompt", system_prompt,
            "--allowedTools", "WebSearch,WebFetch",
            "--permission-mode", "auto",
        ]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
            start_new_session=True,
            env=env,
            limit=sys.maxsize,
        )
        _register_process(proc)

        full_text = ""
        msg_id = f"msg_{int(time.time() * 1000) % 1_000_000:06d}"

        try:
            async with asyncio.timeout(self.config.response_timeout):
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") == "assistant":
                        message = event.get("message", {})
                        for block in message.get("content", []):
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                token = block["text"]
                                full_text += token
                                await ws.send_json({
                                    "type": "sec_stream",
                                    "data": {"token": token, "done": False},
                                })
                            elif block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                status_map = {
                                    "WebSearch": "웹 검색 중...",
                                    "WebFetch": "웹 페이지 가져오는 중...",
                                }
                                status = status_map.get(tool_name)
                                if status:
                                    await ws.send_json({
                                        "type": "sec_tool_status",
                                        "data": {"status": status, "tool": tool_name},
                                    })

                    elif event.get("type") == "result":
                        result_text = event.get("result", "")
                        if event.get("is_error"):
                            logger.warning("chat_result_error", error=result_text)
                            if not full_text:
                                full_text = f"죄송합니다, 응답 생성 중 오류가 발생했습니다: {result_text}"
                                await ws.send_json({
                                    "type": "sec_stream",
                                    "data": {"token": full_text, "done": False},
                                })
                        elif not full_text and result_text:
                            full_text = result_text
                            await ws.send_json({
                                "type": "sec_stream",
                                "data": {"token": full_text, "done": False},
                            })

        except TimeoutError:
            logger.warning("chat_stream_timeout", timeout=self.config.response_timeout)
            if not full_text:
                full_text = "(응답 시간이 초과되었습니다. 다시 시도해주세요.)"
                await ws.send_json({
                    "type": "sec_stream",
                    "data": {"token": full_text, "done": False},
                })
            await _kill_process_tree(proc)
        finally:
            _unregister_process(proc)

        await ws.send_json({
            "type": "sec_stream",
            "data": {"token": "", "done": True, "message_id": msg_id},
        })

        self.history.append(ChatMessage(role="user", content=user_message, message_id=""))
        self.history.append(ChatMessage(role="assistant", content=full_text, message_id=msg_id))
        self._persist()

        if len(self.history) > self.config.compress_threshold:
            asyncio.create_task(self._compress_history())

        return full_text

    # ── History compression ─────────────────────────

    async def _compress_history(self):
        """Compress older messages into a summary, keeping recent turns intact."""
        keep = self.config.max_history_turns
        if len(self.history) <= keep:
            return

        old_msgs = self.history[:-keep]
        recent = self.history[-keep:]

        lines = []
        for msg in old_msgs:
            role = "User" if msg.role == "user" else "Assistant"
            content = msg.content[:300] + "..." if len(msg.content) > 300 else msg.content
            lines.append(f"[{role}] {content}")

        summary_input = (
            "다음 대화 내용을 3~5문장으로 핵심만 요약하세요. 한국어로 작성.\n\n"
            + "\n".join(lines)
        )

        try:
            compress_cmd = [
                "claude", "-p", summary_input,
                "--output-format", "json",
                "--model", "haiku",
                "--max-turns", "1",
            ]
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            proc = await asyncio.create_subprocess_exec(
                *compress_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/tmp",
                start_new_session=True,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            result = json.loads(stdout.decode("utf-8", errors="replace"))
            summary_text = result.get("result", "")

            if summary_text:
                summary_msg = ChatMessage(
                    role="system",
                    content=f"[이전 대화 요약] {summary_text}",
                    message_id="summary",
                )
                self.history = [summary_msg] + recent
                self._persist()
                logger.info("history_compressed: %d -> %d msgs", len(old_msgs) + len(recent), len(self.history))
        except Exception as e:
            logger.warning("history_compress_failed: %s", e)
