"""execution_streamer 단위 테스트.

핵심 전략:
  - 진짜 subprocess 대신 fake proc factory를 주입
  - fake proc은 stream-json 라인을 yield하는 async iterator를 가진 stdout
  - on_event 콜백이 받은 이벤트를 list에 모아 검증
"""

from __future__ import annotations

import json
from typing import List

import pytest

from src.skill_builder.execution_streamer import stream_skill_execution


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream([])
        self.pid = 99999
        self.returncode = 0

    def kill(self) -> None:
        pass

    async def wait(self):
        return 0


def _line(d: dict) -> bytes:
    return (json.dumps(d) + "\n").encode("utf-8")


async def test_stream_emits_started_and_completed() -> None:
    lines = [
        _line({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "안녕"}],
            },
        }),
        _line({"type": "result", "result": "안녕", "is_error": False}),
    ]

    async def fake_factory(*args, **kwargs):
        return _FakeProc(lines)

    events: List[dict] = []
    full_text, tool_count, timed_out = await stream_skill_execution(
        prompt="요약해줘",
        system_prompt="당신은 요약가입니다",
        model="sonnet",
        allowed_tools=["Read", "Write"],
        cwd="/tmp",
        timeout=5,
        on_event=lambda e: events.append(e),
        _proc_factory=fake_factory,
    )

    assert "안녕" in full_text
    assert tool_count == 0
    assert timed_out is False
    assert events[0]["action"] == "started"
    assert events[-1]["action"] == "completed"


async def test_stream_emits_tool_use_events() -> None:
    lines = [
        _line({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {}},
                ],
            },
        }),
        _line({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {}},
                    {"type": "text", "text": "끝"},
                ],
            },
        }),
        _line({"type": "result", "result": "끝", "is_error": False}),
    ]

    async def fake_factory(*args, **kwargs):
        return _FakeProc(lines)

    events: List[dict] = []
    full_text, tool_count, timed_out = await stream_skill_execution(
        prompt="x",
        system_prompt="x",
        model="sonnet",
        allowed_tools=["Read", "Write"],
        cwd="/tmp",
        timeout=5,
        on_event=lambda e: events.append(e),
        _proc_factory=fake_factory,
    )

    tool_events = [e for e in events if e["action"] == "tool_use"]
    assert len(tool_events) == 2
    assert tool_events[0]["tool"] == "Read"
    assert tool_events[1]["tool"] == "Write"
    assert tool_count == 2
    assert "끝" in full_text


async def test_stream_handles_result_error_block() -> None:
    lines = [
        _line({
            "type": "result",
            "result": "내부 오류 발생",
            "is_error": True,
        }),
    ]

    async def fake_factory(*args, **kwargs):
        return _FakeProc(lines)

    events: List[dict] = []
    full_text, _tc, timed_out = await stream_skill_execution(
        prompt="x",
        system_prompt="x",
        model="sonnet",
        allowed_tools=[],
        cwd="/tmp",
        timeout=5,
        on_event=lambda e: events.append(e),
        _proc_factory=fake_factory,
    )

    assert "내부 오류" in full_text
    assert timed_out is False
