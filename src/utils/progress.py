"""Progress tracking for real-time TUI dashboard.

Includes:
- WorkerProgressTracker: per-worker progress during worker_execution
- StepProgressTracker: pipeline-level step progress across all graph nodes

Thread-safe: written by worker/graph threads, read by main event loop for rendering.
Progress estimation uses exponential decay: fast start, natural slowdown near timeout.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum


class WorkerStatus(Enum):
    PENDING = "pending"
    WAITING = "waiting"    # Waiting for dependency stage to complete
    RUNNING = "running"
    TIER2 = "tier2"
    DONE = "done"
    FAILED = "failed"


@dataclass
class WorkerProgress:
    domain: str
    worker_id: str = ""  # unique key (e.g., "deep_researcher_0"); defaults to domain
    worker_name: str = ""  # human-readable name (e.g., "글로벌 AI 시장 리서처")
    role_type: str = ""  # planner/executor/reviewer
    status: WorkerStatus = WorkerStatus.PENDING
    tier: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    tier_changed_at: float = 0.0
    summary: str = ""
    real_progress: float = -1.0  # -1 = use simulation, 0..1 = actual turn-based progress

    @property
    def display_name(self) -> str:
        """Human-readable display name for TUI/WebSocket progress."""
        name = self.worker_name or self.domain
        if self.role_type and self.role_type != "executor":
            return f"{name} [{self.role_type}]"
        return name


# ── Progress estimation ──────────────────────────────

# Time constants (tau) for exponential curve: progress = 1 - e^(-t/tau)
# At t=tau → ~63%, at t=2*tau → ~86%, at t=3*tau → ~95%
_TAU_TIER1 = 150.0   # T1: reaches ~86% at 5min, ~95% at 7.5min
_TAU_TIER2 = 50.0    # T2: reaches ~86% at 1.7min, ~95% at 2.5min (120s timeout)
_MAX_SIMULATED = 0.95  # Never show 100% unless actually done


def compute_progress(w: WorkerProgress, now: float) -> float:
    """Return worker progress.

    If real_progress is set (>= 0), use actual turn-based value.
    Otherwise fall back to exponential decay simulation.
    """
    if w.status == WorkerStatus.DONE:
        return 1.0
    if w.status == WorkerStatus.FAILED:
        if w.real_progress >= 0:
            return w.real_progress
        if w.finished_at > 0 and w.tier_changed_at > 0:
            elapsed = w.finished_at - w.tier_changed_at
            tau = _TAU_TIER2 if w.tier >= 2 else _TAU_TIER1
            return min(_MAX_SIMULATED, 1.0 - math.exp(-elapsed / tau))
        return _MAX_SIMULATED
    if w.status in (WorkerStatus.PENDING, WorkerStatus.WAITING):
        return 0.0

    # RUNNING or TIER2 — use real progress if available
    if w.real_progress >= 0:
        return min(_MAX_SIMULATED, w.real_progress)

    # Fallback: exponential decay simulation (only when no real data)
    phase_start = w.tier_changed_at or w.started_at
    if phase_start <= 0:
        return 0.0

    elapsed = now - phase_start
    tau = _TAU_TIER2 if w.status == WorkerStatus.TIER2 else _TAU_TIER1
    return min(_MAX_SIMULATED, 1.0 - math.exp(-elapsed / tau))


class WorkerProgressTracker:
    """Thread-safe tracker for worker execution progress.

    Written by worker_execution_node (LangGraph thread pool) and
    read by the main asyncio event loop for Rich Live dashboard rendering.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, WorkerProgress] = {}
        self._started_at: float = 0.0
        self._active = False

    def start(self, workers: list[str] | list[tuple] | list[dict]) -> None:
        """Initialize tracking for a batch of workers.

        Args:
            workers: One of:
                - list of domain strings (legacy)
                - list of (worker_id, domain) tuples
                - list of dicts with worker_id, worker_domain, worker_name, role_type
        """
        with self._lock:
            self._started_at = time.time()
            self._active = True
            self._workers = {}
            for item in workers:
                if isinstance(item, dict):
                    wid = item.get("worker_id", item.get("worker_domain", ""))
                    domain = item.get("worker_domain", "")
                    self._workers[wid] = WorkerProgress(
                        domain=domain,
                        worker_id=wid,
                        worker_name=item.get("worker_name", ""),
                        role_type=item.get("role_type", ""),
                    )
                elif isinstance(item, tuple):
                    wid, domain = item
                    self._workers[wid] = WorkerProgress(domain=domain, worker_id=wid)
                else:
                    wid, domain = item, item
                    self._workers[wid] = WorkerProgress(domain=domain, worker_id=wid)

    def update(
        self,
        worker_id: str,
        status: WorkerStatus,
        tier: int = 0,
        summary: str = "",
    ) -> None:
        """Update a single worker's progress (thread-safe).

        Args:
            worker_id: Unique worker key (e.g., "deep_researcher_0").
                       For single-domain workers this equals the domain name.
        """
        with self._lock:
            w = self._workers.get(worker_id)
            if not w:
                return

            # Track phase transitions for progress bar timing
            if status == WorkerStatus.RUNNING and w.started_at == 0.0:
                w.started_at = time.time()
                w.tier_changed_at = time.time()
            elif status == WorkerStatus.TIER2 and w.status != WorkerStatus.TIER2:
                w.tier_changed_at = time.time()

            w.status = status
            w.tier = tier
            if summary:
                w.summary = summary
            if status in (WorkerStatus.DONE, WorkerStatus.FAILED):
                w.finished_at = time.time()

    def set_real_progress(self, worker_id: str, progress: float) -> None:
        """Set actual turn-based progress for a worker (thread-safe).

        Called from agentic loop callback: progress = turn / max_turns.
        """
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.real_progress = max(0.0, min(1.0, progress))

    def stop(self) -> None:
        """Mark tracking session as inactive."""
        with self._lock:
            self._active = False

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def snapshot(self) -> tuple[list[WorkerProgress], float]:
        """Return a snapshot of all workers and total elapsed seconds."""
        with self._lock:
            now = time.time()
            workers = [
                WorkerProgress(
                    domain=w.domain,
                    worker_id=w.worker_id,
                    worker_name=w.worker_name,
                    role_type=w.role_type,
                    status=w.status,
                    tier=w.tier,
                    started_at=w.started_at,
                    finished_at=w.finished_at,
                    tier_changed_at=w.tier_changed_at,
                    summary=w.summary,
                    real_progress=w.real_progress,
                )
                for w in self._workers.values()
            ]
            elapsed = now - self._started_at if self._started_at else 0.0
        return workers, elapsed


# Session-aware tracker registry (supports concurrent Company sessions)
_tracker_registry: dict[str, WorkerProgressTracker] = {}
_tracker_registry_lock = threading.Lock()
_DEFAULT_SESSION = "__default__"


def get_tracker(session_id: str = "") -> WorkerProgressTracker:
    """Get a worker progress tracker for the given session.

    Args:
        session_id: Session identifier. Empty string returns the default
                    tracker (backward-compatible with TUI mode).
    """
    key = session_id or _DEFAULT_SESSION
    with _tracker_registry_lock:
        if key not in _tracker_registry:
            _tracker_registry[key] = WorkerProgressTracker()
        return _tracker_registry[key]


# ── Pipeline Step Progress ─────────────────────────────


class StepStatus(Enum):
    RUNNING = "running"
    DONE = "done"


@dataclass
class StepProgress:
    step_number: int
    node_name: str
    label: str
    status: StepStatus
    started_at: float = 0.0
    finished_at: float = 0.0


# Expected duration (tau) per node for exponential decay curve.
# tau=0 means no progress bar (user-wait or delegated to worker dashboard).
_NODE_TAU: dict[str, float] = {
    "intake": 2,
    "ceo_questions": 20,
    "await_user_answers": 0,
    "single_session": 0,
    "user_review_results": 0,
    "error_terminal": 1,
}

# Node display labels (shared between guards.py decorator and main.py UI)
NODE_LABELS: dict[str, str] = {
    "intake": "작업 접수",
    "ceo_questions": "명확화 질문 생성",
    "await_user_answers": "사용자 답변 대기",
    "single_session": "AI가 정보를 수집하고 보고서를 작성하고 있습니다",
    "user_review_results": "사용자 결과 리뷰",
    "error_terminal": "오류 종료",
}


def compute_step_progress(step: StepProgress, now: float) -> float:
    """Estimate step progress using exponential decay (same formula as workers)."""
    if step.status == StepStatus.DONE:
        return 1.0
    tau = _NODE_TAU.get(step.node_name, 30)
    if tau <= 0:
        return 0.0
    elapsed = now - step.started_at if step.started_at > 0 else 0.0
    if elapsed <= 0:
        return 0.0
    return min(_MAX_SIMULATED, 1.0 - math.exp(-elapsed / tau))


class StepProgressTracker:
    """Thread-safe tracker for pipeline step progress.

    Maintains ordered list of completed + current steps for the Live panel.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steps: list[StepProgress] = []
        self._pipeline_started: float = 0.0

    def start_pipeline(self) -> None:
        """Reset for a new pipeline run."""
        with self._lock:
            self._steps = []
            self._pipeline_started = time.time()

    def begin_step(self, node_name: str, label: str) -> int:
        """Mark previous step DONE, start a new RUNNING step. Returns step number."""
        with self._lock:
            now = time.time()
            # Auto-finish previous running step
            for s in self._steps:
                if s.status == StepStatus.RUNNING:
                    s.status = StepStatus.DONE
                    s.finished_at = now
            step_num = len(self._steps) + 1
            self._steps.append(StepProgress(
                step_number=step_num,
                node_name=node_name,
                label=label,
                status=StepStatus.RUNNING,
                started_at=now,
            ))
            return step_num

    def finish_current(self) -> None:
        """Mark the last running step as DONE."""
        with self._lock:
            now = time.time()
            for s in self._steps:
                if s.status == StepStatus.RUNNING:
                    s.status = StepStatus.DONE
                    s.finished_at = now

    def snapshot(self) -> tuple[list[StepProgress], float]:
        """Return a copy of steps + total pipeline elapsed time."""
        with self._lock:
            now = time.time()
            steps = [
                StepProgress(
                    step_number=s.step_number,
                    node_name=s.node_name,
                    label=s.label,
                    status=s.status,
                    started_at=s.started_at,
                    finished_at=s.finished_at,
                )
                for s in self._steps
            ]
            elapsed = now - self._pipeline_started if self._pipeline_started else 0.0
        return steps, elapsed


# Session-aware step tracker registry
_step_tracker_registry: dict[str, StepProgressTracker] = {}
_step_tracker_registry_lock = threading.Lock()


def get_step_tracker(session_id: str = "") -> StepProgressTracker:
    """Get a step progress tracker for the given session.

    Args:
        session_id: Session identifier. Empty string returns the default
                    tracker (backward-compatible with TUI mode).
    """
    key = session_id or _DEFAULT_SESSION
    with _step_tracker_registry_lock:
        if key not in _step_tracker_registry:
            _step_tracker_registry[key] = StepProgressTracker()
        return _step_tracker_registry[key]
