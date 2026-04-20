"""Headless graph runner for scheduled job execution.

Executes the full 17-node enterprise agent graph without human interaction.
Uses pre-context for automatic answer/escalation resolution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # .env → os.environ (MCP servers need API keys in process env)

from src.engine import ResumeCommand, SqliteCheckpointer

from src.config.settings import Settings
from src.graphs.main_graph import build_pipeline
from src.models.state import create_initial_state
from src.scheduler.models import ExecutionRecord, ExecutionStatus, ScheduledJob
from src.utils.notifier import notify_completion
from src.utils.tracing import configure_tracing, get_run_config

logger = logging.getLogger(__name__)


class HeadlessGraphRunner:
    """Runs the enterprise agent graph without CLI interaction.

    For scheduled execution, all human-in-the-loop points are resolved
    automatically using pre-context data.
    """

    def __init__(self, checkpoint_db_path: str = "data/checkpoints.db"):
        self._checkpoint_db_path = checkpoint_db_path
        configure_tracing()

    async def execute_job(self, job: ScheduledJob) -> ExecutionRecord:
        """Execute a scheduled job through the full graph pipeline.

        Args:
            job: The scheduled job definition with task and pre-context.

        Returns:
            ExecutionRecord with status, duration, and results.
        """
        execution_id = str(uuid.uuid4())[:12]
        thread_id = f"sched-{job.job_id}-{execution_id}"
        started_at = datetime.now(timezone.utc)

        record = ExecutionRecord(
            execution_id=execution_id,
            job_id=job.job_id,
            status=ExecutionStatus.RUNNING,
            started_at=started_at,
            thread_id=thread_id,
        )

        try:
            record = await asyncio.wait_for(
                self._run_graph(job, record, thread_id),
                timeout=job.max_execution_time_seconds,
            )
        except asyncio.TimeoutError:
            record.status = ExecutionStatus.TIMEOUT
            record.error_message = (
                f"Execution timed out after {job.max_execution_time_seconds}s"
            )
            record.completed_at = datetime.now(timezone.utc)
            record.duration_seconds = (
                record.completed_at - started_at
            ).total_seconds()
            logger.error(
                "Job %s execution %s timed out after %ds",
                job.job_id, execution_id, job.max_execution_time_seconds,
            )
        except asyncio.CancelledError:
            # CancelledError is BaseException in Python 3.9+ — finalize record
            # before re-raising so it doesn't stay stuck in RUNNING.
            record.status = ExecutionStatus.TIMEOUT
            record.error_message = "Execution cancelled"
            record.completed_at = datetime.now(timezone.utc)
            record.duration_seconds = (
                record.completed_at - started_at
            ).total_seconds()
            logger.warning(
                "Job %s execution %s cancelled",
                job.job_id, execution_id,
            )
            raise
        except Exception as exc:
            record.status = ExecutionStatus.FAILED
            record.error_message = f"{type(exc).__name__}: {exc}"
            record.completed_at = datetime.now(timezone.utc)
            record.duration_seconds = (
                record.completed_at - started_at
            ).total_seconds()
            logger.exception(
                "Job %s execution %s failed: %s",
                job.job_id, execution_id, exc,
            )

        await self._notify(job, record)
        return record

    @staticmethod
    async def _notify(job: ScheduledJob, record: ExecutionRecord) -> None:
        """스케줄러 완료 알림. 상태별로 성공/실패/타임아웃 구분."""
        status_map: dict[ExecutionStatus, str] = {
            ExecutionStatus.COMPLETED: "success",
            ExecutionStatus.FAILED: "failure",
            ExecutionStatus.TIMEOUT: "timeout",
        }
        status = status_map.get(record.status, "failure")
        title = (getattr(job, "name", None) or getattr(job, "user_task", "") or job.job_id)[:80]
        summary = record.error_message or "정기 실행 완료"
        await notify_completion(
            kind="scheduler",
            title=title,
            summary=summary[:200],
            duration_seconds=record.duration_seconds,
            status=status,  # type: ignore[arg-type]
        )

    async def _run_graph(
        self, job: ScheduledJob, record: ExecutionRecord, thread_id: str,
    ) -> ExecutionRecord:
        """Internal: compile graph and stream through all nodes."""
        started_at = record.started_at
        config = get_run_config(
            thread_id,
            mode="scheduled",
            tags=[f"job:{job.job_id}"],
        )

        async with SqliteCheckpointer(
            self._checkpoint_db_path
        ) as checkpointer:
            app = build_pipeline(checkpointer=checkpointer)

            # Load domain plugins (same as main.py)
            try:
                from src.config.settings import get_settings
                settings = get_settings()
                if settings.enable_plugins:
                    from src.config.plugin_loader import load_and_merge_plugins
                    load_and_merge_plugins(settings.plugin_dir)
            except Exception:
                pass  # Plugin loading is non-critical

            initial_state = create_initial_state(
                user_task=job.user_task,
                execution_mode="scheduled",
                pre_context=job.pre_context.model_dump(),
                session_id=thread_id,
            )

            # Stream through graph
            async for event in app.astream(
                initial_state, config=config,
            ):
                self._log_event(job.job_id, event)

            # Handle any unexpected interrupts (defensive loop)
            max_auto_resumes = 10
            for _ in range(max_auto_resumes):
                snapshot = await app.aget_state(config)
                if not snapshot.next:
                    break  # Graph completed

                # Auto-resume unexpected interrupts with a safe default
                logger.warning(
                    "Job %s: unexpected interrupt at %s, auto-resuming",
                    job.job_id, snapshot.next,
                )
                resume_value = self._build_auto_resume(snapshot, job)
                async for event in app.astream(
                    ResumeCommand(value=resume_value),
                    config=config,
                ):
                    self._log_event(job.job_id, event)

            # Extract final state
            final_snapshot = await app.aget_state(config)
            final_state = final_snapshot.values

        # Build record from final state
        completed_at = datetime.now(timezone.utc)
        final_phase = final_state.get("phase", "unknown")
        final_report = final_state.get("final_report", {})
        error_msg = final_state.get("error_message", "")

        if final_phase == "error":
            record.status = ExecutionStatus.FAILED
            record.error_message = error_msg or "Graph ended in error phase"
        else:
            record.status = ExecutionStatus.COMPLETED

        record.completed_at = completed_at
        record.duration_seconds = (completed_at - started_at).total_seconds()
        record.final_report = final_report if final_report else None
        record.final_state_summary = self._summarize_state(final_state)

        return record

    @staticmethod
    def _build_auto_resume(snapshot, job: ScheduledJob) -> dict | str:
        """Build a safe auto-resume value for unexpected interrupts."""
        # Try to extract interrupt data to determine the type
        for task in snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                interrupt_data = task.interrupts[0].value
                if isinstance(interrupt_data, dict):
                    itype = interrupt_data.get("type", "")
                    if itype == "clarifying_questions":
                        # Supply answers from pre-context
                        questions = interrupt_data.get("questions", {})
                        default = job.pre_context.default_answer
                        answers = {}
                        for domain, q_list in questions.items():
                            domain_answers = job.pre_context.domain_answers.get(domain)
                            if domain_answers:
                                answers[domain] = domain_answers
                            else:
                                answers[domain] = [default] * len(q_list)
                        return answers

                    if itype == "escalation" or interrupt_data.get("escalation_reason"):
                        policy = job.pre_context.escalation_policy
                        if policy == "auto_cancel":
                            return "Cancel and stop execution"
                        choice = job.pre_context.escalation_choice
                        return choice or "Proceed with the current plans"

        # Fallback: generic proceed
        return "Proceed with the current plans"

    @staticmethod
    def _log_event(job_id: str, event: dict) -> None:
        """Log graph update events for observability."""
        for node_name, update in event.items():
            # Skip non-dict updates (e.g. __interrupt__ sends tuples)
            if not isinstance(update, dict):
                continue
            phase = update.get("phase", "")
            messages = update.get("messages", [])
            for msg in messages:
                content = msg.content if hasattr(msg, "content") else str(msg)
                logger.info("[%s] %s | %s", job_id, node_name, content[:200])
            if not messages and phase:
                logger.info("[%s] %s -> phase: %s", job_id, node_name, phase)

    @staticmethod
    def _summarize_state(state: dict) -> dict:
        """Create a compact summary of the final graph state."""
        workers = state.get("workers", [])
        workers_info = []
        for w in workers:
            workers_info.append({
                "domain": w.get("worker_domain", ""),
                "status": w.get("status", ""),
            })

        # report_file_path → URL 경로로 변환
        report_path = state.get("report_file_path", "")
        url_path = ""
        if report_path:
            from pathlib import PurePosixPath
            parts = PurePosixPath(report_path).parts
            try:
                idx = parts.index("reports")
                url_path = "/reports/" + "/".join(parts[idx + 1:])
            except ValueError:
                url_path = report_path

        return {
            "phase": state.get("phase", ""),
            "execution_mode": state.get("execution_mode", ""),
            "workers": workers_info,
            "has_final_report": bool(state.get("final_report")),
            "error_message": state.get("error_message", ""),
            "report_path": url_path,
        }
