"""Scheduler domain models - jobs, executions, pre-context."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ScheduleType(StrEnum):
    CRON = "cron"
    INTERVAL = "interval"


class JobStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ScheduleConfig(BaseModel):
    """Schedule configuration supporting cron and interval."""

    schedule_type: ScheduleType
    cron_expression: str | None = None  # e.g. "0 9 * * MON"
    interval_seconds: int | None = None
    timezone: str = "Asia/Seoul"


class PreContext(BaseModel):
    """Pre-supplied context for headless graph execution."""

    background: str = ""
    domain_answers: dict[str, list[str]] = Field(default_factory=dict)
    default_answer: str = "배경 정보를 기반으로 최선의 판단으로 진행하세요."
    escalation_policy: str = "auto_proceed"  # auto_proceed | auto_cancel | log_and_proceed
    escalation_choice: str | None = None
    strategy: dict | None = None  # 분석 전략 프리셋 (싱글 세션에서 사용)
    previous_report_path: str | None = None  # Delta 비교용: 이전 실행 보고서 경로


class ScheduledJob(BaseModel):
    """A registered periodic task."""

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    name: str
    description: str = ""
    user_task: str
    schedule: ScheduleConfig
    pre_context: PreContext = Field(default_factory=PreContext)
    status: JobStatus = JobStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    max_execution_time_seconds: int = 1800  # 30 min default
    tags: list[str] = Field(default_factory=list)


class ExecutionRecord(BaseModel):
    """Record of a single execution of a scheduled job."""

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    job_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    final_report: dict[str, Any] | None = None
    final_state_summary: dict[str, Any] | None = None
    error_message: str | None = None
    thread_id: str = ""
