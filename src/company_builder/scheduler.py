"""Company Builder scheduler — bridges heartbeat schedules to SchedulerService.

Converts company schedule JSON into ScheduledJob objects and registers them
with the existing SchedulerService. Execution uses HeadlessGraphRunner
through the standard _job_callback flow.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.company_builder import schedule_storage
from src.scheduler.models import (
    JobStatus,
    PreContext,
    ScheduleConfig,
    ScheduledJob,
    ScheduleType,
)

logger = logging.getLogger(__name__)


def register_company_schedules(scheduler_service, user_id: str) -> int:
    """Load all enabled schedules for a user and register with SchedulerService.

    Returns the number of schedules registered.
    """
    schedules = schedule_storage.list_schedules(user_id)
    count = 0
    for sched in schedules:
        if not sched.get("enabled", False):
            continue
        try:
            job = _to_scheduled_job(sched, user_id)
            scheduler_service.add_job(job)
            count += 1
        except Exception as e:
            logger.warning("Failed to register schedule %s: %s", sched.get("id"), e)
    return count


def register_all_company_schedules(scheduler_service) -> int:
    """Scan data/users/*/schedules/ and register every enabled schedule.

    Used on server startup to recover UI-saved schedules that live outside
    the SchedulerService's own SQLite store.
    """
    from pathlib import Path
    users_dir = Path("data/users")
    if not users_dir.exists():
        return 0
    total = 0
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        if not (user_dir / "schedules").exists():
            continue
        total += register_company_schedules(scheduler_service, user_dir.name)
    return total


def register_single_schedule(scheduler_service, user_id: str, schedule_id: str) -> bool:
    """Register a single schedule by ID. Returns True if successful."""
    sched = schedule_storage.load_schedule(user_id, schedule_id)
    if not sched or not sched.get("enabled", False):
        return False
    try:
        job = _to_scheduled_job(sched, user_id)
        scheduler_service.add_job(job)
        return True
    except Exception as e:
        logger.warning("Failed to register schedule %s: %s", schedule_id, e)
        return False


def unregister_schedule(scheduler_service, schedule_id: str) -> None:
    """Remove a schedule from the active scheduler."""
    try:
        scheduler_service.remove_job(f"company_{schedule_id}")
    except Exception:
        pass


def _find_previous_report(sched: dict[str, Any]) -> str | None:
    """run_history에서 마지막 성공 실행의 보고서 파일 경로를 반환.

    MD 파일을 우선 반환 (CLI가 읽기 효율적).
    날짜 파일명 패턴(results_YYYY-MM-DD.md)도 지원.
    """
    import glob
    import os
    for rec in reversed(sched.get("run_history", [])):
        if rec.get("status") != "completed":
            continue
        rp = rec.get("report_path", "")
        if not rp:
            continue
        # URL 경로 → 파일시스템 경로 변환 (/reports/xxx → data/reports/xxx)
        if rp.startswith("/reports/"):
            fs_path = "data" + rp
        else:
            fs_path = rp
        if os.path.isdir(fs_path):
            # 1) 날짜 패턴 MD 파일 (최신순)
            md_dated = sorted(glob.glob(os.path.join(fs_path, "results_*.md")), reverse=True)
            if md_dated:
                return md_dated[0]
            # 2) 일반 파일명
            for fname in ("results.md", "results.html", "results.csv", "results.json"):
                candidate = os.path.join(fs_path, fname)
                if os.path.isfile(candidate):
                    return candidate
        elif os.path.isfile(fs_path):
            return fs_path
    return None


def _to_scheduled_job(sched: dict[str, Any], user_id: str) -> ScheduledJob:
    """Convert a company schedule dict into a ScheduledJob for SchedulerService."""
    from src.company_builder.storage import load_company, load_strategy

    cron_expr = sched.get("cron_expression", "0 9 * * *")
    company_id = sched.get("company_id", "")
    strategy_id = sched.get("strategy_id", "")
    task_desc = sched.get("task_description", "")

    user_task = task_desc

    # 전략 기반 스케줄: strategy를 pre_context에 주입
    strategy_data = None
    if strategy_id:
        strategy = load_strategy(user_id, strategy_id)
        if strategy:
            strategy_data = strategy

    # 팀 기반 스케줄 (레거시 호환)
    team_context = ""
    domain_answers: dict[str, list[str]] = {}
    if company_id and not strategy_id:
        if company_id:
            user_task = f"[Company: {company_id}] {task_desc}"
        company = load_company(user_id, company_id)
        if company and company.get("agents"):
            agents = company["agents"]
            team_lines = []
            for a in agents:
                role_type = a.get("role_type", "executor")
                team_lines.append(
                    f"- {a.get('name', '?')} [{role_type}]: {a.get('role', '?')} (도구: {a.get('tool_category', '?')})"
                )
            team_context = "\n".join(team_lines)
            domains = list({a.get("tool_category", "research") for a in agents})
            for d in domains:
                domain_answers[d] = ["팀 구조에 따라 최선의 판단으로 진행하세요."]

    background = f"이 작업은 자동 스케줄로 실행됩니다. 사용자: {user_id}"
    if strategy_data:
        background += f"\n\n## 분석 전략: {strategy_data.get('name', '')}"
    elif team_context:
        background += f"\n\n## 팀 구성\n{team_context}"

    # 상세 설명 또는 레거시 명확화 답변을 컨텍스트에 포함
    detail = sched.get("detail_description", "")
    if detail:
        background += f"\n\n## 상세 설명\n{detail}"
    else:
        clarify_answers = sched.get("clarify_answers", [])
        if clarify_answers:
            background += "\n\n## 사용자 사전 답변"
            for qa in clarify_answers:
                q = qa.get("question", "")
                a = qa.get("answer", "")
                if q and a:
                    background += f"\nQ: {q}\nA: {a}"

    # Build pre_context — strategy, delta, append 포함
    extra_fields = {}
    if strategy_data:
        extra_fields["strategy"] = strategy_data

    # 이전 보고서 경로 (Delta 비교용)
    prev_report = _find_previous_report(sched)
    if prev_report:
        extra_fields["previous_report_path"] = prev_report

    # 출력 형식 (html / pdf / markdown / csv / json)
    output_format = sched.get("output_format", "html")
    if output_format != "html":
        extra_fields["output_format"] = output_format

    pre_context = PreContext(
        background=background,
        default_answer="스케줄 자동 실행이므로 최선의 판단으로 진행하세요.",
        escalation_policy="auto_proceed",
        domain_answers=domain_answers,
        **extra_fields,
    )

    return ScheduledJob(
        job_id=f"company_{sched['id']}",
        name=sched.get("name", task_desc[:50]),
        description=f"Company heartbeat: {task_desc}",
        user_task=user_task,
        schedule=ScheduleConfig(
            schedule_type=ScheduleType.CRON,
            cron_expression=cron_expr,
        ),
        pre_context=pre_context,
        status=JobStatus.ACTIVE,
        tags=["company_builder", user_id, company_id],
    )


def create_schedule_from_request(
    user_id: str,
    company_id: str,
    task_description: str,
    cron_expression: str,
    name: str = "",
) -> dict[str, Any]:
    """Create and save a new schedule from a frontend request."""
    sched = {
        "user_id": user_id,
        "company_id": company_id,
        "task_description": task_description,
        "cron_expression": cron_expression,
        "name": name or task_description[:50],
        "enabled": True,
    }
    return schedule_storage.save_schedule(user_id, sched)
