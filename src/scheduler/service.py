"""Scheduler service - APScheduler wrapper for periodic job execution.

Manages job lifecycle: registration, scheduling, execution, and safety guards.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config.settings import Settings, get_settings
from src.scheduler.models import (
    ExecutionStatus,
    JobStatus,
    ScheduleConfig,
    ScheduledJob,
    ScheduleType,
)
from src.scheduler.notifier import Notifier, NotificationEvent
from src.scheduler.runner import HeadlessGraphRunner
from src.scheduler.store import SchedulerStore

logger = logging.getLogger(__name__)


class SchedulerService:
    """Manages scheduled job execution via APScheduler.

    Responsibilities:
    - Register/pause/resume/remove jobs
    - Execute jobs via HeadlessGraphRunner
    - Safety guards: consecutive failure auto-pause, daily token budget
    - Recover orphaned executions on restart
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._store = SchedulerStore(self._settings.scheduler_db_path)
        self._runner = HeadlessGraphRunner(self._settings.checkpoint_db_path)
        self._scheduler = AsyncIOScheduler(
            timezone=self._settings.scheduler_timezone,
            job_defaults={
                "max_instances": 1,
                "misfire_grace_time": self._settings.scheduler_misfire_grace_time,
            },
        )
        self._notifier = Notifier(self._settings)
        self._running = False
        # Async callback for broadcasting run events to UI clients.
        # Signature: async (user_id: str, payload: dict) -> None
        self._event_callback = None

    def set_event_callback(self, cb) -> None:
        """Register a broadcaster invoked before/after each job execution.

        The scheduler fires jobs outside the UI request/response cycle, so
        WebSocket clients can only learn about cron-triggered runs through
        this hook. Pass None to disable.
        """
        self._event_callback = cb

    async def _emit_run_event(
        self,
        job: ScheduledJob,
        event_type: str,
        extra: dict | None = None,
    ) -> None:
        """Fan out a run event to the registered broadcaster, if any."""
        if not self._event_callback:
            return
        user_id = job.tags[1] if len(job.tags) >= 2 else ""
        schedule_id = job.job_id.removeprefix("company_")
        payload = {
            "type": event_type,
            "data": {"schedule_id": schedule_id, **(extra or {})},
        }
        try:
            await self._event_callback(user_id, payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("schedule_event_broadcast_failed: %s", e)

    @property
    def store(self) -> SchedulerStore:
        return self._store

    async def start(self) -> None:
        """Start the scheduler service.

        1. Recover orphaned RUNNING executions from previous crash.
        2. Load all ACTIVE jobs and register them with APScheduler.
        3. Start the scheduler loop.
        """
        logger.info("Starting scheduler service...")

        # Recover orphaned executions (RUNNING -> FAILED)
        self._recover_orphaned_executions()

        # Load active jobs
        active_jobs = self._store.list_jobs(status=JobStatus.ACTIVE)
        for job in active_jobs:
            self._register_with_scheduler(job)
            logger.info("Registered job: %s (%s)", job.name, job.job_id)

        self._scheduler.start()
        self._running = True
        logger.info(
            "Scheduler started with %d active jobs", len(active_jobs),
        )

    async def stop(self) -> None:
        """Gracefully stop the scheduler and clean up subprocesses."""
        if self._running:
            self._scheduler.shutdown(wait=False)  # Don't block indefinitely
            self._running = False

            self._store.close()
            logger.info("Scheduler stopped")

    def add_job(self, job: ScheduledJob) -> ScheduledJob:
        """Register a new scheduled job."""
        self._store.save_job(job)
        if job.status == JobStatus.ACTIVE:
            self._register_with_scheduler(job)
        logger.info("Added job: %s (%s)", job.name, job.job_id)
        return job

    def pause_job(self, job_id: str) -> None:
        """Pause a scheduled job."""
        self._store.update_job_status(job_id, JobStatus.PAUSED)
        try:
            self._scheduler.pause_job(job_id)
        except Exception:
            pass  # Job may not be in scheduler if already paused
        logger.info("Paused job: %s", job_id)

    def resume_job(self, job_id: str) -> None:
        """Resume a paused job."""
        self._store.update_job_status(job_id, JobStatus.ACTIVE)
        job = self._store.get_job(job_id)
        if job:
            try:
                self._scheduler.resume_job(job_id)
            except Exception:
                # Re-register if not in scheduler
                self._register_with_scheduler(job)
        logger.info("Resumed job: %s", job_id)

    def remove_job(self, job_id: str) -> None:
        """Remove a job (soft delete in DB, remove from scheduler)."""
        self._store.update_job_status(job_id, JobStatus.DELETED)
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
        logger.info("Removed job: %s", job_id)

    async def trigger_now(self, job_id: str) -> None:
        """Manually trigger immediate execution of a job."""
        job = self._store.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        logger.info("Manual trigger for job: %s (%s)", job.name, job_id)
        await self._job_callback(job_id)

    # ── Internal ───────────────────────────────────────

    def _register_with_scheduler(self, job: ScheduledJob) -> None:
        """Register a job with APScheduler."""
        trigger = self._build_trigger(job.schedule)

        # Remove existing job if present (idempotent re-registration)
        try:
            self._scheduler.remove_job(job.job_id)
        except Exception:
            pass

        self._scheduler.add_job(
            self._job_callback,
            trigger=trigger,
            id=job.job_id,
            args=[job.job_id],
            name=job.name,
            max_instances=1,
        )

    async def _job_callback(self, job_id: str) -> None:
        """Execute a job — called by APScheduler or trigger_now."""
        job = self._store.get_job(job_id)
        if not job or job.status != JobStatus.ACTIVE:
            logger.warning("Skipping inactive/deleted job: %s", job_id)
            return

        # Safety: check consecutive failures
        failures = self._store.get_consecutive_failures(job_id)
        max_failures = self._settings.scheduler_max_consecutive_failures
        if failures >= max_failures:
            logger.error(
                "Job %s has %d consecutive failures — auto-pausing",
                job_id, failures,
            )
            self.pause_job(job_id)
            self._notifier.notify(
                NotificationEvent.JOB_AUTO_PAUSED,
                f"Job Auto-Paused: {job.name}",
                f"{failures} consecutive failures. Job {job_id} has been paused.",
                job_id=job_id,
            )
            return

        logger.info("Executing job: %s (%s)", job.name, job_id)
        await self._emit_run_event(job, "schedule_running")
        record = await self._runner.execute_job(job)

        # Save execution record
        self._store.save_execution(record)

        # Update JSON schedule file (run_count + run_history) for company_builder jobs
        if job.tags and job.tags[0] == "company_builder":
            try:
                from src.company_builder import schedule_storage as ss

                user_id = job.tags[1] if len(job.tags) >= 2 else ""
                schedule_id = job_id.removeprefix("company_")
                report_path = ""
                if record.final_state_summary:
                    report_path = record.final_state_summary.get("report_path", "")
                status = record.status.value if record.status else "unknown"
                if report_path and status == "running":
                    status = "completed"
                ss.add_run_record(
                    user_id, schedule_id,
                    run_id=record.execution_id,
                    status=status,
                    report_path=report_path,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("schedule_json_update_failed: %s", e)

        await self._emit_run_event(
            job,
            "schedule_run_complete",
            {
                "status": record.status.value if record.status else "unknown",
                "duration_s": round(record.duration_seconds or 0, 1),
            },
        )

        # Post-execution notifications
        if record.status == ExecutionStatus.COMPLETED:
            self._notifier.notify(
                NotificationEvent.EXECUTION_COMPLETED,
                f"Completed: {job.name}",
                f"Execution {record.execution_id} finished in {record.duration_seconds:.0f}s",
                job_id=job_id,
                execution_id=record.execution_id,
            )
        elif record.status == ExecutionStatus.TIMEOUT:
            self._notifier.notify(
                NotificationEvent.EXECUTION_TIMEOUT,
                f"Timeout: {job.name}",
                record.error_message or "Execution timed out",
                job_id=job_id,
                execution_id=record.execution_id,
            )
        elif record.status == ExecutionStatus.FAILED:
            self._notifier.notify(
                NotificationEvent.EXECUTION_FAILED,
                f"Failed: {job.name}",
                record.error_message or "Execution failed",
                job_id=job_id,
                execution_id=record.execution_id,
            )

        # Post-execution: check for auto-pause
        if record.status in (ExecutionStatus.FAILED, ExecutionStatus.TIMEOUT):
            new_failures = self._store.get_consecutive_failures(job_id)
            if new_failures >= max_failures:
                self.pause_job(job_id)
                self._notifier.notify(
                    NotificationEvent.JOB_AUTO_PAUSED,
                    f"Job Auto-Paused: {job.name}",
                    f"{new_failures} consecutive failures. Job {job_id} has been paused.",
                    job_id=job_id,
                )

        logger.info(
            "Job %s execution %s: %s (%.1fs)",
            job_id, record.execution_id, record.status.value,
            record.duration_seconds or 0,
        )

    @staticmethod
    def _build_trigger(config: ScheduleConfig):
        """Convert ScheduleConfig to an APScheduler trigger."""
        if config.schedule_type == ScheduleType.CRON:
            if not config.cron_expression:
                raise ValueError("Cron schedule requires cron_expression")
            # APScheduler의 `from_crontab`은 요일 필드(0-7)를 자체 인덱싱
            # (0=월요일)으로 그대로 저장한다. 그 결과 UNIX 표준 `1`(월)이
            # `1`(화)로 해석돼 모든 주간 스케줄이 하루씩 밀려 발화된다.
            # 이름 기반 요일 ("mon","tue",…)은 올바르게 처리되므로, 번호는
            # 이름으로 변환해서 넘긴다.
            fixed_expr = SchedulerService._normalize_cron_dow(
                config.cron_expression,
            )
            return CronTrigger.from_crontab(
                fixed_expr, timezone=config.timezone,
            )
        elif config.schedule_type == ScheduleType.INTERVAL:
            if not config.interval_seconds:
                raise ValueError("Interval schedule requires interval_seconds")
            return IntervalTrigger(
                seconds=config.interval_seconds, timezone=config.timezone,
            )
        else:
            raise ValueError(f"Unknown schedule type: {config.schedule_type}")

    _DOW_NAME_BY_NUM = {
        "0": "sun", "1": "mon", "2": "tue", "3": "wed",
        "4": "thu", "5": "fri", "6": "sat", "7": "sun",
    }

    @classmethod
    def _translate_dow_token(cls, token: str) -> str:
        """단일 요일 토큰을 번호→이름으로 변환. 이미 이름이면 그대로 반환."""
        t = token.strip()
        if not t:
            return t
        # 쉼표 리스트: "1,3,5"
        if "," in t:
            return ",".join(cls._translate_dow_token(p) for p in t.split(","))
        # 스텝: "*/2", "1/2"
        if "/" in t:
            base, step = t.split("/", 1)
            return f"{cls._translate_dow_token(base)}/{step}"
        # 범위: "1-5"
        if "-" in t:
            a, b = t.split("-", 1)
            return f"{cls._translate_dow_token(a)}-{cls._translate_dow_token(b)}"
        # 와일드카드/물음표
        if t in ("*", "?"):
            return t
        # 단일 번호
        return cls._DOW_NAME_BY_NUM.get(t, t)

    @classmethod
    def _normalize_cron_dow(cls, expr: str) -> str:
        """5-필드 cron 표현의 day-of-week 필드만 이름 기반으로 변환."""
        parts = expr.strip().split()
        if len(parts) != 5:
            # 6-필드(년 포함) 또는 비표준 형식은 그대로 반환
            return expr
        parts[4] = cls._translate_dow_token(parts[4])
        return " ".join(parts)

    def _recover_orphaned_executions(self) -> None:
        """Mark any RUNNING executions from previous crash as FAILED."""
        orphans = self._store.list_executions(status=ExecutionStatus.RUNNING)
        for orphan in orphans:
            self._store.update_execution(
                orphan.execution_id,
                status=ExecutionStatus.FAILED.value,
                error_message="Recovered: scheduler restarted while execution was running",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.warning(
                "Recovered orphan execution: %s (job %s)",
                orphan.execution_id, orphan.job_id,
            )
