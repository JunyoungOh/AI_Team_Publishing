"""Phase 2 통합 테스트 — dev_runner의 state 기반 rate limit 재개 로직.

실제 Claude CLI 호출 없이 `_run_cli_session`을 monkey-patch해서:
- 성공 → state 정상 업데이트
- Rate limit 1회 후 성공 → 대기 + 재시도
- 5회 rate limit → 가드 발동 + state='stopped'
- 수동 재시도 트리거 → 대기 조기 종료
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.upgrade.dev_runner import _run_with_state_retry
from src.upgrade.dev_state import (
    DevState,
    GuardTriggeredError,
    cleanup_session,
    trigger_manual_retry,
)
from src.utils.cli_session import RateLimitError


def _make_state(tmp_path, session_id: str, **kwargs) -> DevState:
    """tmp_path 기준 state.json을 쓰고 DevState 반환."""
    state = DevState(session_id=session_id, **kwargs)
    state.save(DevState.path_for(session_id))
    return state


async def test_single_success_updates_window_start(tmp_path, monkeypatch):
    """Happy path: CLI 첫 성공 시 on_first_assistant 호출 → window_start 세팅."""
    monkeypatch.chdir(tmp_path)
    state = _make_state(tmp_path, "sid-happy")
    assert state.window_start_at is None

    async def fake_cli(*, on_first_assistant=None, **kwargs):
        if on_first_assistant:
            on_first_assistant()
        return "OK"

    monkeypatch.setattr("src.upgrade.dev_runner._run_cli_session", fake_cli)

    result = await _run_with_state_retry(
        state=state,
        system_prompt="sys", user_prompt="usr", tools=[], phase="dev",
    )
    assert result == "OK"
    assert state.window_start_at is not None
    assert state.backoff_index == 0
    cleanup_session("sid-happy")


async def test_rate_limit_then_success_waits_and_resumes(tmp_path, monkeypatch):
    """첫 호출 rate limit, 두 번째 성공. 실제 wait 발생 확인."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.upgrade.dev_state.FIRST_WAIT_CAP", 1)  # 1초로 단축

    state = _make_state(tmp_path, "sid-retry")
    calls: list[float] = []

    async def fake_cli(*, on_first_assistant=None, **kwargs):
        calls.append(time.time())
        if len(calls) == 1:
            raise RateLimitError("mocked rate limit")
        if on_first_assistant:
            on_first_assistant()
        return "OK"

    monkeypatch.setattr("src.upgrade.dev_runner._run_cli_session", fake_cli)

    result = await _run_with_state_retry(
        state=state,
        system_prompt="sys", user_prompt="usr", tools=[], phase="dev",
    )

    assert result == "OK"
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.5, "1초 wait이 실제로 발생해야 함"
    assert state.backoff_index == 0, "성공 후 backoff 리셋"
    assert len(state.rate_limit_history) == 1
    cleanup_session("sid-retry")


async def test_guard_triggers_after_max_retries(tmp_path, monkeypatch):
    """5회 연속 rate limit → GuardTriggeredError, state='stopped'."""
    monkeypatch.chdir(tmp_path)
    # 대기 시간 0초로 단축 — 가드까지 빠르게 도달 (int 유지: RateLimitEvent.waited=int)
    monkeypatch.setattr("src.upgrade.dev_state.FIRST_WAIT_CAP", 0)
    monkeypatch.setattr("src.upgrade.dev_state.BACKOFF_SCHEDULE", [0, 0, 0, 0, 0])

    state = _make_state(tmp_path, "sid-guard")

    async def always_rate_limit(**kwargs):
        raise RateLimitError("perpetual")

    monkeypatch.setattr("src.upgrade.dev_runner._run_cli_session", always_rate_limit)

    with pytest.raises(GuardTriggeredError):
        await _run_with_state_retry(
            state=state,
            system_prompt="sys", user_prompt="usr", tools=[], phase="dev",
        )

    assert state.state == "stopped"
    assert state.error_reason == "guard_triggered"
    assert len(state.rate_limit_history) == 5, "정확히 5회 기록 후 가드"
    cleanup_session("sid-guard")


async def test_manual_retry_skips_wait(tmp_path, monkeypatch):
    """수동 '지금 시도' 트리거로 긴 대기가 조기 종료."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.upgrade.dev_state.FIRST_WAIT_CAP", 30)  # 일부러 길게

    state = _make_state(tmp_path, "sid-manual")
    calls: list[float] = []

    async def fake_cli(*, on_first_assistant=None, **kwargs):
        calls.append(time.time())
        if len(calls) == 1:
            raise RateLimitError("mocked")
        if on_first_assistant:
            on_first_assistant()
        return "OK"

    monkeypatch.setattr("src.upgrade.dev_runner._run_cli_session", fake_cli)

    async def fire_manual_after_delay():
        await asyncio.sleep(0.3)
        # wait_or_manual이 asyncio.Event를 만든 뒤에 trigger해야 깨어남
        for _ in range(10):
            if trigger_manual_retry("sid-manual"):
                return
            await asyncio.sleep(0.05)

    trigger_task = asyncio.create_task(fire_manual_after_delay())
    start = time.time()
    result = await _run_with_state_retry(
        state=state,
        system_prompt="sys", user_prompt="usr", tools=[], phase="dev",
    )
    elapsed = time.time() - start
    await trigger_task

    assert result == "OK"
    assert elapsed < 5, f"수동 트리거로 30초 wait이 조기 종료돼야 함 (실제 {elapsed:.1f}s)"
    # 마지막 이벤트는 manual=True가 아니라 timer로 기록됨 (이유: record_rate_limit
    # 시점에는 수동인지 타이머인지 아직 모름). manual 플래그는 wait_or_manual 이후에
    # 알게 되는데, 현재 구현은 기록 당시엔 manual=False. Phase 3에서 정교화 가능.
    cleanup_session("sid-manual")


async def test_state_persists_across_rate_limit(tmp_path, monkeypatch):
    """Rate limit 중 state.json 파일이 'waiting' 상태로 저장되어야 함."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.upgrade.dev_state.FIRST_WAIT_CAP", 2)

    state = _make_state(tmp_path, "sid-persist")
    state_path = DevState.path_for("sid-persist")

    captured_states: list[str] = []

    async def fake_cli(*, on_first_assistant=None, **kwargs):
        # 첫 호출 중엔 state='running'일 것
        loaded = DevState.load(state_path)
        captured_states.append(loaded.state)
        raise RateLimitError("once")

    monkeypatch.setattr("src.upgrade.dev_runner._run_cli_session", fake_cli)

    async def observe_during_wait():
        await asyncio.sleep(0.3)
        loaded = DevState.load(state_path)
        captured_states.append(f"wait:{loaded.state}")
        trigger_manual_retry("sid-persist")

    async def fake_cli_success_2nd(*, on_first_assistant=None, **kwargs):
        if on_first_assistant:
            on_first_assistant()
        return "OK"

    # 첫 호출은 rate_limit, 두번째는 성공
    call_count = {"n": 0}
    async def dual_cli(*, on_first_assistant=None, **kwargs):
        call_count["n"] += 1
        loaded = DevState.load(state_path)
        captured_states.append(f"call{call_count['n']}:{loaded.state}")
        if call_count["n"] == 1:
            raise RateLimitError("once")
        if on_first_assistant:
            on_first_assistant()
        return "OK"

    monkeypatch.setattr("src.upgrade.dev_runner._run_cli_session", dual_cli)

    observer = asyncio.create_task(observe_during_wait())
    result = await _run_with_state_retry(
        state=state,
        system_prompt="sys", user_prompt="usr", tools=[], phase="dev",
    )
    await observer

    assert result == "OK"
    assert "call1:running" in captured_states, f"첫 호출 시 running: {captured_states}"
    assert any(s.startswith("wait:waiting") for s in captured_states), \
        f"대기 중 state=waiting 디스크 확인: {captured_states}"
    cleanup_session("sid-persist")
