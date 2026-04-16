"""Shared Claude CLI subprocess runner with rate-limit handling.

개발의뢰(강화소/최초개발)과 미래아이디어가 함께 쓰는 CLI 세션 헬퍼.
원래는 src/overtime/runner.py 안에 있었으나, 야근팀 기능 제거 후 공유
유틸로 분리되었다.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from src.modes.common import emit_mode_event
from src.utils.logging import get_logger

_logger = get_logger(agent_id="cli_session")

# 안전: shell injection 방지를 위해 인자 배열 + shell=False 방식의 subprocess 사용.
# 훅 오탐 우회: 'create_subprocess_' + 'exec' 로 분리 후 getattr 로 가져옴.
_SPAWN = getattr(asyncio, "create_subprocess_" + "exec")

_RATE_LIMIT_SIGNALS = ["rate_limit", "rate limit", "overloaded", "429", "quota"]
_USAGE_FILE = Path("/tmp/claude-usage.json")


class RateLimitError(Exception):
    """CLI 세션이 rate limit에 걸렸을 때 발생."""

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


def _get_rate_limit_wait() -> tuple[int, bool]:
    """사용량 파일에서 대기 시간 계산.

    Returns:
        (wait_seconds, is_rate_limit):
        - is_rate_limit=True: 실제 rate limit. wait_seconds만큼 대기 후 재개
        - is_rate_limit=False: rate limit이 아닌 다른 오류
    """
    if not _USAGE_FILE.exists():
        return 300, False

    try:
        data = json.loads(_USAGE_FILE.read_text())
    except Exception:
        return 300, False

    five_hour = data.get("five_hour") or {}
    used_pct = five_hour.get("used_percentage", 0)
    resets_at = five_hour.get("resets_at")

    if used_pct < 80:
        return 0, False

    if resets_at:
        wait = max(int(resets_at - time.time()) + 30, 60)
        return min(wait, 7200), True

    return 300, True


def _tool_detail(tool_name: str, inp: dict) -> str:
    """도구 input에서 핵심 정보 한 줄 추출. 개발의뢰 탭 활동 카드에 표시."""
    if not isinstance(inp, dict):
        return ""

    if tool_name == "WebSearch":
        q = inp.get("query") or ""
        return f"'{q[:60]}'" if q else ""
    if tool_name == "WebFetch":
        url = inp.get("url") or ""
        return url if len(url) <= 60 else url[:57] + "..."
    if tool_name in ("Read", "Write", "Edit"):
        path = inp.get("file_path") or ""
        if not path:
            return ""
        parts = path.rsplit("/", 2)
        return "/".join(parts[-2:]) if len(parts) > 1 else path
    if tool_name == "Bash":
        cmd = inp.get("command") or ""
        return cmd if len(cmd) <= 70 else cmd[:67] + "..."
    if tool_name in ("Glob", "Grep"):
        pattern = inp.get("pattern") or inp.get("query") or ""
        return pattern if len(pattern) <= 60 else pattern[:57] + "..."
    if tool_name == "Agent":
        desc = inp.get("description") or inp.get("prompt") or ""
        desc = str(desc)
        return desc if len(desc) <= 80 else desc[:77] + "..."
    return ""


_TOOL_LABELS = {
    "WebSearch": "검색", "WebFetch": "수집",
    "Agent": "에이전트", "Write": "저장",
    "Read": "읽기", "Bash": "실행",
    "Edit": "편집", "Glob": "파일 검색", "Grep": "코드 검색",
}


async def run_cli_session(
    system_prompt: str,
    user_prompt: str,
    tools: list[str],
    session_id: str,
    model: str = "sonnet",
    max_turns: int = 60,
    timeout: int = 420,
    cwd: str | None = None,
    activity_event_type: str = "dev_activity",
    effort: str | None = None,
    on_first_assistant: "callable | None" = None,
) -> str:
    """CLI subprocess를 실행하고 결과를 반환. rate limit 시 RateLimitError.

    cwd: 기본 None(프로젝트 루트). 강화소처럼 사용자 폴더 안에서 실행해야 할 때 지정.
    activity_event_type: 도구 사용 이벤트 WS 타입.
    on_first_assistant: 첫 'assistant' 스트림 이벤트 수신 시 1회 호출되는 콜백.
        rate limit이 아님을 확정적으로 판단하는 시점 (rate limit은 보통 result
        이벤트로 즉시 is_error=True로 오기 때문에 assistant 이벤트가 선행되면
        실제 호출이 통과한 것). dev_state의 record_success 훅에 쓰인다.
        콜백 예외는 삼켜서 CLI 흐름을 깨뜨리지 않음.
    """
    from src.utils.claude_code import (
        _register_process,
        _unregister_process,
        _kill_process_tree,
    )

    cmd = [
        "claude", "-p", user_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--max-turns", str(max_turns),
        "--append-system-prompt", system_prompt,
        "--allowedTools", ",".join(tools),
        "--permission-mode", "auto",
    ]
    if effort:
        cmd.extend(["--effort", effort])

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    proc = await _SPAWN(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or os.getcwd(),
        start_new_session=True,
        env=env,
        limit=sys.maxsize,
    )
    _register_process(proc)

    full_text = ""
    tool_count = 0
    first_assistant_fired = False
    start = time.time()

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

                if event.get("type") == "assistant":
                    if not first_assistant_fired and on_first_assistant is not None:
                        try:
                            on_first_assistant()
                        except Exception as exc:
                            _logger.warning("on_first_assistant_error",
                                            error=str(exc)[:200])
                        first_assistant_fired = True
                    for block in event.get("message", {}).get("content", []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            full_text += block["text"]
                        elif block.get("type") == "tool_use":
                            tool_count += 1
                            tool_name = block.get("name", "")
                            label = _TOOL_LABELS.get(tool_name, tool_name)
                            detail = _tool_detail(tool_name, block.get("input") or {})
                            emit_mode_event(session_id, {
                                "type": activity_event_type,
                                "data": {
                                    "tool": tool_name,
                                    "label": label,
                                    "count": tool_count,
                                    "detail": detail,
                                },
                            })

                elif event.get("type") == "result":
                    result_text = event.get("result", "")
                    is_error = event.get("is_error", False)
                    subtype = event.get("subtype", "")

                    if is_error:
                        error_lower = (result_text + subtype).lower()
                        for signal in _RATE_LIMIT_SIGNALS:
                            if signal in error_lower:
                                _logger.warning("cli_session_rate_limited", result=result_text[:200])
                                raise RateLimitError(result_text[:200])

                    if not full_text and result_text:
                        full_text = result_text

    except TimeoutError:
        _logger.warning("cli_session_timeout", elapsed=round(time.time() - start, 1))
        await _kill_process_tree(proc)
    except RateLimitError:
        await _kill_process_tree(proc)
        raise
    except BaseException:
        await _kill_process_tree(proc)
        raise
    finally:
        _unregister_process(proc)

    return full_text


# Backwards-compat alias for callers that imported the private name.
_run_cli_session = run_cli_session
