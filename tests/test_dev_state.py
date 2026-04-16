"""개발의뢰 세션 상태(dev_state) 단위 테스트.

커버리지:
- DevState 저장/로드 (원자적 쓰기, 손상 파일 처리)
- compute_wait: 첫 회 추정 + 캡, 지수 backoff
- check_guard / guard_remaining: 6h 롤링 윈도우
- record_success / record_rate_limit: 상태 전이
- 세션 락 + 수동 이벤트 registry + wait_or_manual
- scan_all_states / mark_orphans_as_error
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from src.upgrade.dev_state import (
    BACKOFF_SCHEDULE,
    FIRST_WAIT_CAP,
    GUARD_MAX_RETRIES,
    WINDOW_SEC,
    DevState,
    RateLimitEvent,
    check_guard,
    cleanup_session,
    compute_wait,
    get_lock,
    get_manual_event,
    guard_remaining,
    mark_orphans_as_error,
    recent_failures,
    scan_all_states,
    trigger_manual_retry,
    wait_or_manual,
)


# ── compute_wait ─────────────────────────────

def test_compute_wait_no_window_start_returns_cap():
    """window_start 모르면 무조건 30분 캡."""
    state = DevState(session_id="t", backoff_index=1)
    assert compute_wait(state, now=10_000) == FIRST_WAIT_CAP


def test_compute_wait_window_near_end_returns_short():
    """윈도우 끝 5분 전 rate limit → ~5분+60초 (캡 안 침)."""
    now = 10_000
    state = DevState(
        session_id="t",
        window_start_at=now - (WINDOW_SEC - 5 * 60),
        backoff_index=1,
    )
    wait = compute_wait(state, now=now)
    assert 5 * 60 <= wait <= 5 * 60 + 60 + 1
    assert wait < FIRST_WAIT_CAP


def test_compute_wait_early_in_window_caps_at_30min():
    """윈도우 시작 30분 후 rate limit → 추정 4.5h인데 캡으로 30분만."""
    now = 10_000
    state = DevState(
        session_id="t",
        window_start_at=now - 30 * 60,
        backoff_index=1,
    )
    assert compute_wait(state, now=now) == FIRST_WAIT_CAP


def test_compute_wait_window_expired_returns_zero():
    """윈도우 이미 지남 → 0 (즉시 재시도). 재시도 후 또 실패면 backoff로 전환됨."""
    now = 10_000
    state = DevState(
        session_id="t",
        window_start_at=now - 6 * 3600,
        backoff_index=1,
    )
    assert compute_wait(state, now=now) == 0


def test_compute_wait_backoff_schedule():
    """2~6회차는 고정 스케줄 (15m, 30m, 60m, 120m, 120m)."""
    for i, expected in enumerate(BACKOFF_SCHEDULE, start=2):
        state = DevState(session_id="t", backoff_index=i)
        assert compute_wait(state, now=10_000) == expected, f"backoff_index={i}"


def test_compute_wait_backoff_caps_at_last():
    """스케줄 끝난 뒤엔 마지막 값 유지 (무한 backoff 방지)."""
    state = DevState(session_id="t", backoff_index=20)
    assert compute_wait(state, now=10_000) == BACKOFF_SCHEDULE[-1]


# ── check_guard / recent_failures ─────────────────────────────

def _rl_event(seconds_ago: float, now: float = 10_000) -> RateLimitEvent:
    return RateLimitEvent(at=now - seconds_ago, waited=0, manual=False)


def test_guard_false_when_empty():
    state = DevState(session_id="t")
    assert check_guard(state, now=10_000) is False
    assert guard_remaining(state, now=10_000) == GUARD_MAX_RETRIES


def test_guard_counts_only_within_6h():
    now = 10_000
    state = DevState(
        session_id="t",
        rate_limit_history=[
            _rl_event(100, now),        # 최근
            _rl_event(3 * 3600, now),   # 3h 전
            _rl_event(7 * 3600, now),   # 7h 전 — 가드 윈도우 밖
            _rl_event(8 * 3600, now),   # 8h 전 — 밖
        ],
    )
    assert recent_failures(state, now) == 2
    assert guard_remaining(state, now) == GUARD_MAX_RETRIES - 2
    assert check_guard(state, now) is False


def test_guard_triggers_at_max():
    now = 10_000
    state = DevState(
        session_id="t",
        rate_limit_history=[_rl_event(i * 600, now) for i in range(GUARD_MAX_RETRIES)],
    )
    assert check_guard(state, now) is True
    assert guard_remaining(state, now) == 0


# ── record_success / record_rate_limit ─────────────────────────────

def test_record_success_sets_window_start_when_none():
    state = DevState(session_id="t", backoff_index=3)
    state.record_success(now=5_000)
    assert state.window_start_at == 5_000
    assert state.backoff_index == 0
    assert state.next_retry_at is None


def test_record_success_keeps_window_if_still_active():
    """윈도우 진행 중(< 5h) 호출이면 window_start 유지."""
    state = DevState(session_id="t", window_start_at=1_000, backoff_index=2)
    state.record_success(now=1_000 + 3 * 3600)  # 3h 경과 — 아직 안 만료
    assert state.window_start_at == 1_000
    assert state.backoff_index == 0


def test_record_success_rolls_over_expired_window():
    """윈도우 만료 후 호출 → 새 윈도우 시작점으로 갱신."""
    state = DevState(session_id="t", window_start_at=1_000)
    new_now = 1_000 + WINDOW_SEC + 100
    state.record_success(now=new_now)
    assert state.window_start_at == new_now


def test_record_rate_limit_first_returns_cap_no_window():
    """첫 rate limit (window_start 모름) → FIRST_WAIT_CAP 반환, state='waiting'."""
    state = DevState(session_id="t", state="running", backoff_index=0)
    wait = state.record_rate_limit(now=5_000)
    assert wait == FIRST_WAIT_CAP
    assert state.state == "waiting"
    assert state.backoff_index == 1
    assert state.next_retry_at == 5_000 + FIRST_WAIT_CAP
    assert len(state.rate_limit_history) == 1
    assert state.rate_limit_history[0].manual is False
    assert state.rate_limit_history[0].waited == FIRST_WAIT_CAP


def test_record_rate_limit_second_uses_backoff():
    """두 번째 rate limit → BACKOFF_SCHEDULE[0] (15분) 반환."""
    state = DevState(session_id="t", backoff_index=1)  # 이미 1회 있음
    wait = state.record_rate_limit(now=10_000)
    assert wait == BACKOFF_SCHEDULE[0]
    assert state.backoff_index == 2


def test_record_rate_limit_manual_flag_preserved():
    state = DevState(session_id="t")
    state.record_rate_limit(now=5_000, manual=True)
    assert state.rate_limit_history[0].manual is True


def test_record_rate_limit_multiple_increments():
    state = DevState(session_id="t")
    state.record_rate_limit(now=1_000)
    state.record_rate_limit(now=2_000)
    state.record_rate_limit(now=3_000)
    assert state.backoff_index == 3
    assert len(state.rate_limit_history) == 3


# ── save / load ─────────────────────────────

def test_save_load_round_trip(tmp_path: Path):
    # 실시간 기준 상대값 — save() 시 _prune_history가 24h 전 기록을 잘라내므로
    now = time.time()
    state = DevState(
        session_id="abc123",
        task="할일 관리 앱",
        answers="로컬 저장",
        state="waiting",
        window_start_at=now - 1_000,
        backoff_index=2,
        next_retry_at=now + 1_800,
        rate_limit_history=[RateLimitEvent(at=now - 500, waited=1800, manual=False)],
    )
    path = tmp_path / "state.json"
    state.save(path)

    loaded = DevState.load(path)
    assert loaded.session_id == "abc123"
    assert loaded.task == "할일 관리 앱"
    assert loaded.state == "waiting"
    assert loaded.backoff_index == 2
    assert loaded.window_start_at == pytest.approx(now - 1_000)
    assert len(loaded.rate_limit_history) == 1


def test_save_is_atomic_no_partial_file(tmp_path: Path):
    """저장 후 .tmp 파일 잔존 없음 — replace 완료 확인."""
    state = DevState(session_id="atomic")
    path = tmp_path / "state.json"
    state.save(path)
    tmp = path.with_suffix(".json.tmp")
    assert path.exists()
    assert not tmp.exists()


def test_save_updates_updated_at(tmp_path: Path):
    state = DevState(session_id="t", created_at=1_000, updated_at=1_000)
    before = time.time()
    state.save(tmp_path / "s.json")
    assert state.updated_at >= before


def test_load_corrupted_raises(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        DevState.load(path)


def test_save_prunes_old_history(tmp_path: Path):
    """24시간 넘은 이벤트는 save 시 잘려나감."""
    now = time.time()
    state = DevState(
        session_id="t",
        rate_limit_history=[
            RateLimitEvent(at=now - 1 * 3600, waited=0),    # 최근
            RateLimitEvent(at=now - 30 * 3600, waited=0),   # 30h 전 — 잘림
        ],
    )
    state.save(tmp_path / "s.json")
    loaded = DevState.load(tmp_path / "s.json")
    assert len(loaded.rate_limit_history) == 1


# ── 락 + 수동 이벤트 registry ─────────────────────────────

def test_get_lock_returns_same_instance():
    cleanup_session("lock-test")
    a = get_lock("lock-test")
    b = get_lock("lock-test")
    assert a is b
    cleanup_session("lock-test")


def test_cleanup_session_removes_registry_entries():
    get_lock("cleanup-test")
    get_manual_event("cleanup-test")
    cleanup_session("cleanup-test")
    # 다시 가져오면 새 인스턴스
    new_lock = get_lock("cleanup-test")
    assert new_lock.locked() is False
    cleanup_session("cleanup-test")


def test_trigger_manual_retry_returns_false_for_unknown_session():
    cleanup_session("ghost-session")
    assert trigger_manual_retry("ghost-session") is False


async def test_wait_or_manual_returns_timer_on_timeout():
    cleanup_session("timer-test")
    result = await wait_or_manual("timer-test", wait_sec=0.05)
    assert result == "timer"
    cleanup_session("timer-test")


async def test_wait_or_manual_returns_manual_when_event_set():
    sid = "manual-test"
    cleanup_session(sid)
    # 미리 event 생성 (wait_or_manual 안에서 생기므로 여기선 get으로 선 등록)
    get_manual_event(sid)

    async def fire():
        await asyncio.sleep(0.05)
        trigger_manual_retry(sid)

    asyncio.create_task(fire())
    start = time.time()
    result = await wait_or_manual(sid, wait_sec=10)
    elapsed = time.time() - start
    assert result == "manual"
    assert elapsed < 1, "수동 트리거 후 즉시 깨어나야 함"
    cleanup_session(sid)


async def test_wait_or_manual_clears_stale_event():
    """이전 세션의 stale event가 남아있어도 다음 wait가 즉시 빠지지 않아야 함."""
    sid = "stale-test"
    cleanup_session(sid)
    event = get_manual_event(sid)
    event.set()  # 이전에 세팅된 상태로 시뮬레이션
    result = await wait_or_manual(sid, wait_sec=0.05)
    assert result == "timer"  # 즉시 manual이 아니라 정상 타임아웃
    cleanup_session(sid)


# ── scan + orphan cleanup ─────────────────────────────

def _make_state_file(root: Path, session_id: str, **fields) -> Path:
    d = root / session_id
    d.mkdir(parents=True)
    state = DevState(session_id=session_id, **fields)
    path = d / "state.json"
    state.save(path)
    return path


def test_scan_all_states_empty_when_no_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert scan_all_states() == []


def test_scan_all_states_skips_corrupted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data/workspace/overtime/output"
    _make_state_file(root, "good1", state="running")
    _make_state_file(root, "good2", state="done")

    # 손상된 파일
    bad_dir = root / "bad1"
    bad_dir.mkdir(parents=True)
    (bad_dir / "state.json").write_text("{broken", encoding="utf-8")

    results = scan_all_states()
    ids = {s.session_id for s in results}
    assert ids == {"good1", "good2"}  # 손상된 것 skip


def test_mark_orphans_converts_running_and_waiting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "data/workspace/overtime/output"
    _make_state_file(root, "s-run", state="running")
    _make_state_file(root, "s-wait", state="waiting")
    _make_state_file(root, "s-done", state="done")
    _make_state_file(root, "s-stopped", state="stopped")

    count = mark_orphans_as_error()
    assert count == 2  # running + waiting만

    all_states = {s.session_id: s for s in scan_all_states()}
    assert all_states["s-run"].state == "error"
    assert all_states["s-run"].error_reason == "server_restart"
    assert all_states["s-wait"].state == "error"
    assert all_states["s-done"].state == "done"
    assert all_states["s-stopped"].state == "stopped"
