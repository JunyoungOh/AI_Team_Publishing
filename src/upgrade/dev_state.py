"""개발의뢰 모드 세션 상태 영속화 + rate limit 재개 로직.

역할:
- state.json 로드/저장 (세션별 영속 상태, 원자적 쓰기)
- compute_wait(): rate limit 대기 시간 계산
  - 첫 회: window_start 추정 + 30분 캡
  - 이후: 지수 backoff (15m → 30m → 60m → 120m → 120m)
- check_guard(): 6시간 내 5회 이상 실패 시 자동 중지
- 세션 단위 asyncio.Lock + 수동 재시도 asyncio.Event registry

state.json 위치: data/workspace/overtime/output/{session_id}/state.json
(work_dir인 .../app/ 바깥 — 사용자 앱 파일과 분리)
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# ── 튜닝 파라미터 ─────────────────────────────
WINDOW_SEC = 5 * 3600           # Claude 5h 윈도우
FIRST_WAIT_CAP = 30 * 60        # 첫 대기 상한 (window_start 추정이 빗나가도 30분이면 재시도)
BACKOFF_SCHEDULE = [15 * 60, 30 * 60, 60 * 60, 120 * 60, 120 * 60]  # 2~6회차 대기
GUARD_WINDOW_SEC = 6 * 3600     # 가드 롤링 윈도우
GUARD_MAX_RETRIES = 5           # 이 숫자 이상 실패 → 자동 중지
HISTORY_PRUNE_SEC = 24 * 3600   # state.json 저장 시 이보다 오래된 기록은 잘라냄

STATE_VALUES = ("pending", "running", "waiting", "stopped", "done", "error")
StateLiteral = Literal["pending", "running", "waiting", "stopped", "done", "error"]


class RateLimitEvent(BaseModel):
    at: float
    waited: int = 0
    manual: bool = False


class DevState(BaseModel):
    """개발의뢰 세션 영속 상태 — state.json의 1:1 모델."""

    session_id: str
    state: StateLiteral = "pending"
    user_id: str = ""

    # 세션 레시피 (재개 시 CLI 재구성용)
    task: str = ""
    answers: str = ""
    workspace_files: list[str] = Field(default_factory=list)
    work_dir: str = ""

    # 세션 진행 상태
    session_number: int = 0
    handoff_context: str = ""
    dev_complete: bool = False
    phase: str = ""

    # Rate limit 추적
    window_start_at: float | None = None
    rate_limit_history: list[RateLimitEvent] = Field(default_factory=list)
    backoff_index: int = 0
    next_retry_at: float | None = None

    # 오류 정보
    error_reason: str = ""

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # ── 영속화 ─────────────────────────────

    @classmethod
    def path_for(cls, session_id: str, workspace_mode: str = "overtime") -> Path:
        return Path(f"data/workspace/{workspace_mode}/output/{session_id}/state.json")

    @classmethod
    def load(cls, path: Path) -> "DevState":
        """손상된 state.json은 ValidationError 올림 — 호출부에서 error로 마크."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def save(self, path: Path | None = None) -> None:
        """원자적 쓰기: tmp 파일에 먼저 쓰고 os.replace로 rename."""
        target = path or self.path_for(self.session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = time.time()
        self._prune_history()
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)

    def _prune_history(self) -> None:
        """24시간 넘은 rate limit 이벤트는 제거 (가드는 6시간만 보지만 디버깅용 여유)."""
        cutoff = time.time() - HISTORY_PRUNE_SEC
        self.rate_limit_history = [e for e in self.rate_limit_history if e.at >= cutoff]

    # ── 상태 변화 기록 ─────────────────────────────

    def record_success(self, now: float) -> None:
        """CLI 첫 성공 응답 수신 시 호출. window_start 롤오버 + backoff 리셋."""
        if self.window_start_at is None or now - self.window_start_at > WINDOW_SEC:
            self.window_start_at = now
        self.backoff_index = 0
        self.next_retry_at = None

    def record_rate_limit(self, now: float, manual: bool = False) -> int:
        """RateLimitError 잡힌 직후 호출. 다음 대기 시간(초)을 반환.

        순서: backoff_index 증가 → 그 상태 기준으로 compute_wait 호출 →
        history 기록 + next_retry_at + state='waiting' 전환.
        이 순서가 중요한 이유: compute_wait이 갱신된 backoff_index를 봐야
        "이번이 N번째 rate limit"에 맞는 대기 시간을 돌려주기 때문.
        """
        self.backoff_index += 1
        wait_sec = compute_wait(self, now)
        self.rate_limit_history.append(
            RateLimitEvent(at=now, waited=wait_sec, manual=manual)
        )
        self.next_retry_at = now + wait_sec
        self.state = "waiting"
        return wait_sec


# ── 순수 함수: 대기 계산 + 가드 ─────────────────────────────

def compute_wait(state: DevState, now: float) -> int:
    """다음 재시도까지 대기 초. backoff_index는 '이번까지 누적된 rate limit 횟수'."""
    if state.backoff_index <= 1:
        # 첫 rate limit: window_start 기반 추정, 30분 캡
        if state.window_start_at is not None:
            estimated = max(0, int((state.window_start_at + WINDOW_SEC + 60) - now))
            return min(estimated, FIRST_WAIT_CAP)
        return FIRST_WAIT_CAP

    # 2회차 이후: 지수 backoff
    idx = min(state.backoff_index - 2, len(BACKOFF_SCHEDULE) - 1)
    return BACKOFF_SCHEDULE[idx]


def recent_failures(state: DevState, now: float) -> int:
    return sum(1 for e in state.rate_limit_history if now - e.at < GUARD_WINDOW_SEC)


def guard_remaining(state: DevState, now: float) -> int:
    return max(0, GUARD_MAX_RETRIES - recent_failures(state, now))


def check_guard(state: DevState, now: float) -> bool:
    """True면 더 이상 재시도 금지 — 6h 내 GUARD_MAX_RETRIES 도달."""
    return recent_failures(state, now) >= GUARD_MAX_RETRIES


# ── 세션 단위 락 + 수동 트리거 이벤트 레지스트리 ─────────────────────────────
# 같은 이벤트 루프 내에서만 유효 (단일 서버 프로세스 가정). 멀티프로세스 시 재설계 필요.

_locks: dict[str, asyncio.Lock] = {}
_manual_events: dict[str, asyncio.Event] = {}


def get_lock(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def get_manual_event(session_id: str) -> asyncio.Event:
    event = _manual_events.get(session_id)
    if event is None:
        event = asyncio.Event()
        _manual_events[session_id] = event
    return event


def trigger_manual_retry(session_id: str) -> bool:
    """수동 '지금 시도' WS 메시지 처리용. 대기 중인 세션이 있으면 True."""
    event = _manual_events.get(session_id)
    if event is None:
        return False
    event.set()
    return True


async def wait_or_manual(session_id: str, wait_sec: int) -> str:
    """타이머 만료 OR 수동 이벤트 중 먼저 오는 쪽으로 깨어남.

    Returns:
        'manual' — 수동 버튼, 'timer' — 정상 만료, 'cancelled' — 외부 취소
    """
    event = get_manual_event(session_id)
    event.clear()
    try:
        await asyncio.wait_for(event.wait(), timeout=wait_sec)
        return "manual"
    except asyncio.TimeoutError:
        return "timer"
    except asyncio.CancelledError:
        # 상위 태스크 취소 (stop_dev 등) — 전파
        raise


def cleanup_session(session_id: str) -> None:
    """세션 종료 시 메모리 정리."""
    _locks.pop(session_id, None)
    _manual_events.pop(session_id, None)


# ── 활성 세션 스캔 (재접속 + 부팅 복구용) ─────────────────────────────

def scan_all_states(workspace_mode: str = "overtime") -> list[DevState]:
    """모든 state.json을 읽어 리스트로 반환. 손상된 파일은 건너뜀."""
    root = Path(f"data/workspace/{workspace_mode}/output")
    if not root.exists():
        return []
    results: list[DevState] = []
    for state_file in root.glob("*/state.json"):
        try:
            results.append(DevState.load(state_file))
        except Exception:
            continue
    return results


def mark_orphans_as_error(workspace_mode: str = "overtime") -> int:
    """서버 부팅 시 호출. running/waiting 상태 state.json을 error로 전환.

    이전 서버 프로세스가 죽으면서 남긴 "진행 중" 세션은 실제로는 asyncio.Task가
    사라진 상태 — 복구 불가능하므로 명시적으로 error 마크.

    Returns: 정리된 세션 수
    """
    count = 0
    for state in scan_all_states(workspace_mode):
        if state.state in ("running", "waiting"):
            state.state = "error"
            state.error_reason = "server_restart"
            state.save()
            count += 1
    return count


class GuardTriggeredError(Exception):
    """6h/5회 가드 발동 — 자동 재개 중지."""
