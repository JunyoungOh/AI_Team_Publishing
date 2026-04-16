"""Common interfaces shared by all execution modes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# API-native tool names (web_search/web_fetch are Anthropic server-side tools)
TOOL_CATEGORY_MAP: dict[str, list[str]] = {
    "research": ["web_search", "web_fetch", "firecrawl_scrape"],
    "verify": ["web_search"],
    "none": [],
}


class ModeParticipant(BaseModel):
    """A participant in a mode execution with persona and tool access."""

    name: str
    persona: str
    role: str
    tool_category: Literal["research", "verify", "none"] = "none"


class ModeResult(BaseModel):
    """Common result wrapper returned by all execution modes."""

    mode: Literal["roundtable", "adversarial", "workshop", "relay"]
    summary: str
    result_html: str
    quality_score: float = Field(ge=0, le=10)
    roundtable: dict[str, Any] | None = None
    adversarial: dict[str, Any] | None = None
    workshop: dict[str, Any] | None = None
    relay: dict[str, Any] | None = None


import asyncio

_mode_event_queues: dict[str, asyncio.Queue] = {}

def get_mode_event_queue(session_id: str) -> asyncio.Queue:
    if session_id not in _mode_event_queues:
        _mode_event_queues[session_id] = asyncio.Queue()
    return _mode_event_queues[session_id]

def emit_mode_event(session_id: str, event: dict):
    queue = _mode_event_queues.get(session_id)
    if queue:
        queue.put_nowait(event)

def cleanup_mode_event_queue(session_id: str):
    _mode_event_queues.pop(session_id, None)


async def run_task_with_stop_listener(
    ws,
    task: "asyncio.Task",
    stop_types: set[str],
    on_message=None,
) -> str:
    """Run `task` while concurrently listening for stop messages on `ws`.

    Solves the "blocked receive loop" problem: a WebSocket endpoint that
    directly `await`s a long-running task cannot simultaneously process stop
    messages, because control never returns to `ws.receive_json()` until the
    task completes. This helper spawns a parallel listener; if a message with
    `type` in `stop_types` arrives, `task` is cancelled and cleanup runs.

    Non-stop messages are passed to `on_message(msg)` if provided. This lets
    endpoints handle mid-flight signals like "manual_retry" without stopping
    the task. Callback exceptions are swallowed so listener can't crash.

    Returns:
        "completed" — task finished normally.
        "stopped" — a stop message was received and task was cancelled.

    Raises:
        The exception from `ws.receive_json()` (e.g. WebSocketDisconnect) if
        the socket fails during listening; `task` is cancelled first.
        The exception from `task` itself, if any, after listener cleanup.
    """
    async def _listen():
        while True:
            msg = await ws.receive_json()
            if msg.get("type") in stop_types:
                return
            if on_message is not None:
                try:
                    on_message(msg)
                except Exception:
                    pass

    listener = asyncio.create_task(_listen())

    try:
        done, _pending = await asyncio.wait(
            {task, listener},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        # Outer cancellation (e.g. endpoint torn down) — clean up both children
        listener.cancel()
        task.cancel()
        raise

    if listener in done:
        listener_exc = listener.exception()
        task.cancel()
        try:
            await task
        except BaseException:
            pass  # task's own finally blocks handle subprocess kill
        if listener_exc is not None:
            raise listener_exc
        return "stopped"

    # Task completed first — cancel listener, propagate task exception
    listener.cancel()
    try:
        await listener
    except BaseException:
        pass
    exc = task.exception()
    if exc is not None:
        raise exc
    return "completed"
