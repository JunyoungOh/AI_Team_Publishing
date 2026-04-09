"""Claude Code 서브프로세스를 stream-json으로 실행하고 콜백으로 이벤트 emit.

``single_session._stream_session()`` 패턴을 그대로 복제하되:
  - mode_event_queue 의존성 제거 → on_event 콜백
  - 의존성 주입 가능한 _proc_factory (테스트용)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Awaitable, Callable, Optional


EventCallback = Callable[[dict], None]


async def _default_proc_factory(
    cmd: list[str], *, cwd: str, env: dict[str, str]
):
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
        env=env,
    )


async def stream_skill_execution(
    *,
    prompt: str,
    system_prompt: str,
    model: str,
    allowed_tools: list[str],
    cwd: str,
    timeout: int,
    on_event: EventCallback,
    _proc_factory: Optional[Callable[..., Awaitable]] = None,
) -> tuple[str, int, bool]:
    """스트리밍 실행. Returns: (full_text, tool_count, timed_out)"""
    factory = _proc_factory or _default_proc_factory

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--max-turns", "30",
        "--append-system-prompt", system_prompt,
        "--permission-mode", "auto",
    ]
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    proc = await factory(cmd, cwd=cwd, env=env)

    full_text = ""
    tool_count = 0
    timed_out = False
    start_time = time.time()

    on_event({"action": "started", "elapsed": 0})

    try:
        async with asyncio.timeout(timeout):
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                elapsed = round(time.time() - start_time, 1)

                if event_type == "assistant":
                    message = event.get("message", {})
                    for block in message.get("content", []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            chunk = block.get("text", "")
                            full_text += chunk
                            on_event({
                                "action": "text",
                                "chunk": chunk,
                                "elapsed": elapsed,
                            })
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            tool_count += 1
                            on_event({
                                "action": "tool_use",
                                "tool": tool_name,
                                "tool_count": tool_count,
                                "elapsed": elapsed,
                            })
                elif event_type == "result":
                    result_text = event.get("result", "")
                    if event.get("is_error"):
                        if not full_text:
                            full_text = result_text
                    elif not full_text and result_text:
                        full_text = result_text
    except TimeoutError:
        timed_out = True
        elapsed = round(time.time() - start_time, 1)
        on_event({"action": "timeout", "elapsed": elapsed})
        try:
            proc.kill()
        except Exception:
            pass

    elapsed = round(time.time() - start_time, 1)
    on_event({
        "action": "completed",
        "elapsed": elapsed,
        "tool_count": tool_count,
        "timed_out": timed_out,
    })

    return full_text, tool_count, timed_out
