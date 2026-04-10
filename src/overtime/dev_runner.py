# src/overtime/dev_runner.py
"""야근팀 개발 모드 runner.

흐름:
  1. 명확화 질문 생성 (CLI 1회) → UI로 전달 → 사용자 답변 대기
  2. 개발 세션 실행 (CLI auto 모드, 장시간)
     - rate limit → 사용량 파일에서 리셋 시점 읽어 정확히 대기 후 재개
     - 컨텍스트 한계 → PROGRESS.md 기반 새 세션 handoff
  3. 완료 리포트 생성 (CLI 1회)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from src.config.settings import get_settings
from src.modes.common import emit_mode_event
from src.overtime.dev_prompts import (
    build_clarify_prompt,
    build_dev_system_prompt,
    build_handoff_prompt,
    build_report_prompt,
)
from src.overtime.runner import (
    RateLimitError,
    _run_cli_session,
)
from src.utils.logging import get_logger

_logger = get_logger(agent_id="dev_runner")

_DEV_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent"]
_REPORT_TOOLS = ["Read", "Write", "Bash", "Glob", "Grep"]

MAX_SESSIONS = 10
_USAGE_FILE = Path("/tmp/claude-usage.json")
_COMPLETION_MARKER = "ALL_PHASES_DONE"


def _emit(session_id: str, phase: str, action: str, **kwargs):
    """개발 모드 이벤트 emit 헬퍼."""
    emit_mode_event(session_id, {
        "type": "dev_progress",
        "data": {"phase": phase, "action": action, **kwargs},
    })


def _get_rate_limit_wait() -> tuple[int, bool]:
    """사용량 파일에서 대기 시간 계산.

    Returns:
        (wait_seconds, is_rate_limit):
        - is_rate_limit=True: 실제 rate limit. wait_seconds만큼 대기 후 재개
        - is_rate_limit=False: rate limit이 아닌 다른 오류
    """
    if not _USAGE_FILE.exists():
        return 300, False  # 파일 없으면 판단 불가 → 일반 오류로 처리

    try:
        data = json.loads(_USAGE_FILE.read_text())
    except Exception:
        return 300, False

    five_hour = data.get("five_hour") or {}
    used_pct = five_hour.get("used_percentage", 0)
    resets_at = five_hour.get("resets_at")

    # 사용량 80% 미만이면 rate limit이 아닌 다른 오류
    if used_pct < 80:
        return 0, False

    # resets_at이 있으면 정확한 대기 시간 계산
    if resets_at:
        wait = max(int(resets_at - time.time()) + 30, 60)  # 30초 여유
        return min(wait, 7200), True  # 최대 2시간 캡

    # resets_at 없지만 사용량이 높으면 기본 5분
    return 300, True


async def _run_with_rate_limit_retry(
    system_prompt: str,
    user_prompt: str,
    tools: list[str],
    session_id: str,
    phase: str,
    model: str = "sonnet",
    max_turns: int = 60,
    timeout: int = 420,
    max_non_rl_retries: int = 3,
) -> str:
    """CLI 세션 실행 + 사용량 파일 기반 rate limit 재시도.

    - rate limit: 사용량 파일에서 리셋 시점 읽어 대기 → 무한 재시도
    - 기타 오류: max_non_rl_retries까지만 재시도
    """
    non_rl_attempts = 0

    while True:
        try:
            return await _run_cli_session(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
                session_id=session_id,
                model=model,
                max_turns=max_turns,
                timeout=timeout,
            )
        except RateLimitError as e:
            wait_sec, is_rl = _get_rate_limit_wait()

            if not is_rl:
                # rate limit이 아닌 다른 오류
                non_rl_attempts += 1
                if non_rl_attempts >= max_non_rl_retries:
                    raise
                _emit(session_id, phase, "retry",
                      message=f"일시 오류 — 재시도 ({non_rl_attempts}/{max_non_rl_retries})")
                await asyncio.sleep(30)
                continue

            # 실제 rate limit — 리셋까지 대기
            wait_min = wait_sec // 60
            _logger.warning("dev_rate_limited", phase=phase, wait_s=wait_sec)
            _emit(session_id, phase, "rate_limited",
                  message=f"사용량 한도 도달 — {wait_min}분 후 자동 재개",
                  cooldown=wait_sec,
                  resume_at=int(time.time()) + wait_sec)
            await asyncio.sleep(wait_sec)


async def generate_clarify_questions(
    task: str,
    session_id: str,
) -> str:
    """명확화 질문 생성. bridge.structured_query + Pydantic 스키마로 구조화된 응답 강제."""
    from pydantic import BaseModel, Field
    from src.utils.bridge_factory import get_bridge

    class DevClarifyQuestions(BaseModel):
        """개발 모드 명확화 질문."""
        questions: list[str] = Field(min_length=1, max_length=5, description="3~5개 질문 목록")

    settings = get_settings()
    system, user = build_clarify_prompt(task)

    _emit(session_id, "clarify", "generating", message="명확화 질문 생성 중")

    bridge = get_bridge()
    try:
        result: DevClarifyQuestions = await bridge.structured_query(
            system_prompt=system,
            user_message=user,
            output_schema=DevClarifyQuestions,
            model=settings.worker_model,
            allowed_tools=[],
            timeout=120,
        )
        # 번호 매긴 질문 텍스트로 변환
        lines = [f"{i+1}. {q}" for i, q in enumerate(result.questions)]
        return "\n".join(lines)
    except Exception as e:
        _logger.warning("clarify_structured_error", error=str(e))
        return "질문 생성에 실패했습니다. 자유롭게 설명을 추가해주세요."


async def run_dev_overtime(
    task: str,
    answers: str,
    session_id: str,
    user_id: str = "",
    overtime_id: str = "",
    file_context: str = "",
) -> str:
    """개발 모드 메인 실행. 완료 시 report_dir 반환."""
    from src.company_builder.storage import update_overtime_iteration

    settings = get_settings()
    work_dir = f"data/workspace/overtime/output/{session_id}/app"
    report_dir = f"data/reports/{session_id}"
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    effective_task = task
    if file_context:
        effective_task = task + "\n\n" + file_context

    handoff_context = ""
    dev_complete = False

    # ── 개발 루프 (컨텍스트 한계 시 새 세션으로 이어받기) ──
    for session_num in range(1, MAX_SESSIONS + 1):
        _logger.info("dev_session_start", session=session_num, session_id=session_id)

        system_prompt = build_dev_system_prompt(
            task=effective_task,
            answers=answers,
            work_dir=work_dir,
            handoff_context=handoff_context,
        )
        user_prompt = (
            "위 지시에 따라 앱 개발을 시작하세요. "
            "모든 Phase를 완료할 때까지 자율적으로 진행하세요."
        )
        if handoff_context:
            user_prompt = (
                "이전 세션이 중단된 지점부터 이어서 개발하세요. "
                "먼저 PROGRESS.md를 읽고 현재 상태를 파악하세요."
            )

        _emit(session_id, "dev", "session_start",
              session_number=session_num,
              message=f"개발 세션 #{session_num} 시작")

        # 개발 세션 실행
        try:
            await _run_with_rate_limit_retry(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=_DEV_TOOLS,
                session_id=session_id,
                phase="dev",
                model=settings.worker_model,
                max_turns=200,
                timeout=1800,  # 30분
            )
        except Exception as e:
            _logger.error("dev_session_error", session=session_num, error=str(e))
            _emit(session_id, "dev", "error",
                  message=f"세션 #{session_num} 오류: {str(e)[:200]}")

        # 완료 여부 판단: PROGRESS.md에서 ALL_PHASES_DONE 마커 확인
        progress_file = Path(work_dir) / "PROGRESS.md"

        if progress_file.exists():
            progress_content = progress_file.read_text(encoding="utf-8", errors="replace")
            if _COMPLETION_MARKER in progress_content:
                dev_complete = True
                _emit(session_id, "dev", "complete",
                      message="개발 완료 — 리포트 생성 중")
                break
            else:
                # 아직 진행 중 → handoff
                _emit(session_id, "dev", "handoff",
                      message=f"세션 #{session_num} 종료 — 새 세션으로 이어받기")
                handoff_context = progress_content[-3000:]  # 뒤에서 3000자
                _logger.info("dev_handoff", session=session_num,
                             progress_len=len(progress_content))
        else:
            # PROGRESS.md도 없으면 실패 가능성
            _emit(session_id, "dev", "handoff",
                  message=f"세션 #{session_num} — 진행 파일 없음, 재시도")
            handoff_context = ""

        if overtime_id and user_id:
            update_overtime_iteration(user_id, overtime_id, {
                "id": f"dev_session_{session_num}",
                "action": "session_complete",
                "completed": dev_complete,
            }, status="running")

    if not dev_complete:
        _emit(session_id, "dev", "max_sessions",
              message=f"최대 세션 수({MAX_SESSIONS})에 도달. 현재까지의 결과로 리포트를 생성합니다.")

    # ── 완료 리포트 생성 ──
    _emit(session_id, "report", "generating",
          message="앱 설명 리포트 + 실행 가이드 생성 중")

    report_system, report_user = build_report_prompt(
        task=effective_task, work_dir=work_dir, report_dir=report_dir,
    )

    try:
        await _run_with_rate_limit_retry(
            system_prompt=report_system,
            user_prompt=report_user,
            tools=_REPORT_TOOLS,
            session_id=session_id,
            phase="report",
            model=settings.worker_model,
            max_turns=20,
            timeout=300,
        )
    except Exception as e:
        _logger.error("dev_report_error", error=str(e))

    # 리포트 파일 확인 + 폴백
    report_file = Path(report_dir) / "results.html"
    if not report_file.exists():
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        files = list(Path(work_dir).rglob("*"))
        file_list = "\n".join(
            f"  - {f.relative_to(work_dir)}" for f in files if f.is_file()
        )
        report_file.write_text(
            f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            f"<title>개발 완료</title></head><body style='background:#0D1117;"
            f"color:#E6EDF3;padding:40px;font-family:sans-serif;'>"
            f"<h1>개발 완료</h1>"
            f"<h2>요청</h2><p>{effective_task[:500]}</p>"
            f"<h2>생성된 파일</h2><pre>{file_list}</pre>"
            f"<h2>실행 방법</h2><p>터미널에서 앱 폴더로 이동 후 실행하세요.</p>"
            f"</body></html>",
            encoding="utf-8",
        )

    _emit(session_id, "report", "complete",
          report_path=f"/reports/{session_id}",
          app_dir=work_dir,
          message="리포트 생성 완료")

    if overtime_id and user_id:
        update_overtime_iteration(user_id, overtime_id, {
            "id": "dev_final",
            "action": "completed",
            "report_dir": report_dir,
        }, status="completed")

    return report_dir
