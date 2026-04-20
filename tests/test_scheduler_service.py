"""Unit tests for SchedulerService — trigger building and job management."""

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.scheduler.models import (
    JobStatus,
    ScheduleConfig,
    ScheduleType,
    ScheduledJob,
)
from src.scheduler.service import SchedulerService


def _make_cron_config():
    return ScheduleConfig(
        schedule_type=ScheduleType.CRON,
        cron_expression="0 9 * * MON",
    )


def _make_interval_config():
    return ScheduleConfig(
        schedule_type=ScheduleType.INTERVAL,
        interval_seconds=3600,
    )


# ── _build_trigger ───────────────────────────────


def test_build_cron_trigger():
    trigger = SchedulerService._build_trigger(_make_cron_config())
    assert isinstance(trigger, CronTrigger)


def test_build_interval_trigger():
    trigger = SchedulerService._build_trigger(_make_interval_config())
    assert isinstance(trigger, IntervalTrigger)


def test_build_trigger_cron_missing_expression():
    config = ScheduleConfig(schedule_type=ScheduleType.CRON, cron_expression=None)
    with pytest.raises(ValueError, match="cron_expression"):
        SchedulerService._build_trigger(config)


def test_build_trigger_interval_missing_seconds():
    config = ScheduleConfig(schedule_type=ScheduleType.INTERVAL, interval_seconds=None)
    with pytest.raises(ValueError, match="interval_seconds"):
        SchedulerService._build_trigger(config)


# ── dow 번역 (APScheduler from_crontab dow 오프바이원 회피) ────────────


def test_normalize_cron_dow_single_number():
    # UNIX cron: 1=월, 2=화 ... 0/7=일
    assert SchedulerService._normalize_cron_dow("0 11 * * 1") == "0 11 * * mon"
    assert SchedulerService._normalize_cron_dow("0 11 * * 0") == "0 11 * * sun"
    assert SchedulerService._normalize_cron_dow("0 11 * * 7") == "0 11 * * sun"
    assert SchedulerService._normalize_cron_dow("0 11 * * 6") == "0 11 * * sat"


def test_normalize_cron_dow_wildcards_and_composites():
    assert SchedulerService._normalize_cron_dow("30 10 * * *") == "30 10 * * *"
    assert SchedulerService._normalize_cron_dow("0 9 * * 1-5") == "0 9 * * mon-fri"
    assert SchedulerService._normalize_cron_dow("0 9 * * 1,3,5") == "0 9 * * mon,wed,fri"


def test_normalize_cron_dow_already_named_passthrough():
    assert SchedulerService._normalize_cron_dow("0 9 * * mon") == "0 9 * * mon"
    assert SchedulerService._normalize_cron_dow("0 9 * * MON") == "0 9 * * MON"


def test_cron_trigger_fires_on_correct_weekday():
    """Monday 크론 표현이 실제로 월요일에 발화하는지 검증.

    회귀 방지: APScheduler 3.x의 `from_crontab`이 dow 번호(UNIX: 1=월)를
    자체 인덱싱(0=월)으로 해석하는 오프바이원 버그 때문에, 모든 주간 스케줄이
    의도한 요일 다음날에 발화했던 과거 결함을 잡아둔다.
    """
    from datetime import datetime
    import pytz

    config = ScheduleConfig(
        schedule_type=ScheduleType.CRON,
        cron_expression="0 11 * * 1",  # UNIX cron: 월요일 11:00
        timezone="Asia/Seoul",
    )
    trigger = SchedulerService._build_trigger(config)
    # 2026-04-19 일요일 기준 다음 발화는 2026-04-20 월요일이어야 한다.
    kst = pytz.timezone("Asia/Seoul")
    sunday = kst.localize(datetime(2026, 4, 19))
    next_fire = trigger.get_next_fire_time(None, sunday)
    assert next_fire is not None
    assert next_fire.strftime("%A") == "Monday", (
        f"expected Monday, got {next_fire.strftime('%A')} ({next_fire.isoformat()})"
    )


# ── Job lifecycle via store ──────────────────────


def test_add_and_pause_job(tmp_db_path):
    from src.config.settings import Settings

    settings = Settings(
        scheduler_db_path=tmp_db_path,
        checkpoint_db_path=tmp_db_path.replace("test.db", "cp.db"),
    )
    service = SchedulerService(settings)

    job = ScheduledJob(
        name="Test Job",
        user_task="Do analysis",
        schedule=_make_cron_config(),
    )
    service.add_job(job)

    # Verify job is saved
    retrieved = service.store.get_job(job.job_id)
    assert retrieved is not None
    assert retrieved.status == JobStatus.ACTIVE

    # Pause
    service.pause_job(job.job_id)
    paused = service.store.get_job(job.job_id)
    assert paused.status == JobStatus.PAUSED

    # Resume
    service.resume_job(job.job_id)
    resumed = service.store.get_job(job.job_id)
    assert resumed.status == JobStatus.ACTIVE

    # Remove (soft delete)
    service.remove_job(job.job_id)
    removed = service.store.get_job(job.job_id)
    assert removed.status == JobStatus.DELETED
