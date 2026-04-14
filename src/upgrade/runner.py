"""강화소 runner.

흐름:
  1. analyze_and_clarify(folder, task) — CLI 1회 호출로 앱 분석 + 질문 생성
  2. run_upgrade_dev(folder, task, answers, analysis) — 업그레이드 실행 (장시간)
     - rate limit 시 자동 재개 (overtime/dev_runner의 _run_with_rate_limit_retry 재사용)
     - 컨텍스트 한계 시 PROGRESS_UPGRADE.md 기반 handoff, 최대 MAX_SESSIONS
  3. run_upgrade_report(...) — 완료 리포트 HTML 생성
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from src.config.settings import get_settings
from src.modes.common import emit_mode_event
from src.overtime.dev_runner import _run_with_rate_limit_retry
from src.overtime.runner import _run_cli_session
from src.upgrade.backup import create_backup, validate_target_folder
from src.upgrade.prompts import (
    build_analyze_prompt,
    build_report_prompt,
    build_upgrade_dev_prompt,
)
from src.utils.logging import get_logger

_logger = get_logger(agent_id="upgrade_runner")

_ANALYZE_TOOLS = ["Read", "Glob", "Grep", "Bash"]
_DEV_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "WebSearch", "WebFetch"]
_REPORT_TOOLS = ["Read", "Write", "Bash", "Glob", "Grep"]

MAX_SESSIONS = 10
_COMPLETION_MARKER = "ALL_PHASES_DONE"
_PROGRESS_FILENAME = "PROGRESS_UPGRADE.md"


def _emit(session_id: str, phase: str, action: str, **kwargs):
    """강화소 이벤트 emit 헬퍼."""
    emit_mode_event(session_id, {
        "type": "upgrade_progress",
        "data": {"phase": phase, "action": action, **kwargs},
    })


def _parse_analysis_json(raw_text: str) -> dict:
    """분석 결과 텍스트에서 JSON 블록을 추출.

    Claude가 ```json ... ``` 코드블록을 쓰거나 맨 뒷부분에 JSON만 출력하는 경우 모두 대응.
    """
    text = raw_text.strip()

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    start = text.rfind("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"분석 결과를 JSON으로 파싱할 수 없음: {text[:300]}")


async def prepare_and_analyze(
    folder_path: str,
    task: str,
    session_id: str,
) -> dict:
    """폴더 검증 → 백업 생성 → 앱 분석 + 명확화 질문.

    Returns:
        {
            "folder_path": 사용자 지정 폴더 절대경로,
            "backup_path": 생성된 백업 폴더 경로,
            "analysis": 파싱된 분석 결과 dict,
        }

    Raises:
        ValueError: 폴더 검증 실패
    """
    valid, err = validate_target_folder(folder_path)
    if not valid:
        raise ValueError(err)

    abs_folder = str(Path(folder_path).expanduser().resolve())

    _emit(session_id, "backup", "start", message="백업 생성 중 — 큰 프로젝트는 수십 초 걸릴 수 있어요")
    try:
        backup_path = await asyncio.to_thread(create_backup, abs_folder)
    except Exception as e:
        _logger.error("upgrade_backup_failed", error=str(e))
        raise ValueError(f"백업 생성 실패: {e}")
    _emit(session_id, "backup", "complete", backup_path=backup_path,
          message=f"백업 완료: {backup_path}")

    _emit(session_id, "analyze", "start", message="앱 분석 + 명확화 질문 생성 중")

    system, user = build_analyze_prompt(task)
    settings = get_settings()

    try:
        raw = await _run_cli_session(
            system_prompt=system,
            user_prompt=user,
            tools=_ANALYZE_TOOLS,
            session_id=session_id,
            model=settings.worker_model,
            max_turns=40,
            timeout=300,
            cwd=abs_folder,
            activity_event_type="upgrade_activity",
        )
    except Exception as e:
        _logger.error("upgrade_analyze_failed", error=str(e))
        raise

    try:
        analysis = _parse_analysis_json(raw)
    except ValueError as e:
        _logger.warning("upgrade_analyze_parse_failed", raw=raw[:500])
        analysis = {
            "summary": "앱 구조를 자동으로 파악하지 못했습니다.",
            "stack": [],
            "entry_points": [],
            "questions": [
                "어떤 종류의 앱인가요? (예: 웹앱, 데스크톱, 스크립트)",
                "기존에 이 앱을 실행하는 명령어는 무엇인가요? (예: npm start)",
                "업그레이드 후 기존 기능이 그대로 작동해야 하나요, 아니면 일부 기능은 변경/제거해도 되나요?",
            ],
            "concerns": [],
            "file_count": 0,
        }

    _emit(session_id, "analyze", "complete",
          summary=analysis.get("summary", ""),
          stack=analysis.get("stack", []),
          questions=analysis.get("questions", []),
          concerns=analysis.get("concerns", []))

    return {
        "folder_path": abs_folder,
        "backup_path": backup_path,
        "analysis": analysis,
    }


async def run_upgrade_dev(
    folder_path: str,
    task: str,
    answers: str,
    backup_path: str,
    analysis: dict,
    session_id: str,
) -> str:
    """업그레이드 개발 세션 실행. 야근팀 dev 패턴 차용.

    Returns:
        report_dir 경로
    """
    settings = get_settings()
    report_dir = f"data/reports/{session_id}"
    progress_file = Path(folder_path) / _PROGRESS_FILENAME

    handoff_context = ""
    dev_complete = False

    for session_num in range(1, MAX_SESSIONS + 1):
        _logger.info("upgrade_session_start", session=session_num, session_id=session_id)

        system_prompt = build_upgrade_dev_prompt(
            task=task,
            answers=answers,
            app_summary=analysis.get("summary", ""),
            app_stack=analysis.get("stack", []),
            app_entry_points=analysis.get("entry_points", []),
            app_concerns=analysis.get("concerns", []),
            backup_path=backup_path,
            handoff_context=handoff_context,
        )
        user_prompt = (
            "위 지시에 따라 앱 업그레이드를 시작하세요. "
            "Phase 1부터 Phase 4까지 모두 완료할 때까지 자율적으로 진행하세요."
        )
        if handoff_context:
            user_prompt = (
                f"이전 세션이 중단된 지점부터 이어서 진행하세요. "
                f"먼저 `{_PROGRESS_FILENAME}`을 읽고 현재 상태를 파악한 뒤, "
                f"미완료 Phase부터 계속하세요."
            )

        _emit(session_id, "dev", "session_start",
              session_number=session_num,
              message=f"업그레이드 세션 #{session_num} 시작")

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
                cwd=folder_path,
                activity_event_type="upgrade_activity",
                emit_event_type="upgrade_progress",
            )
        except Exception as e:
            _logger.error("upgrade_session_error", session=session_num, error=str(e))
            _emit(session_id, "dev", "error",
                  message=f"세션 #{session_num} 오류: {str(e)[:200]}")

        if progress_file.exists():
            progress_content = progress_file.read_text(encoding="utf-8", errors="replace")
            if _COMPLETION_MARKER in progress_content:
                dev_complete = True
                _emit(session_id, "dev", "complete",
                      message="업그레이드 완료 — 리포트 생성 중")
                break
            else:
                _emit(session_id, "dev", "handoff",
                      message=f"세션 #{session_num} 종료 — 새 세션으로 이어받기")
                handoff_context = progress_content[-3000:]
                _logger.info("upgrade_handoff", session=session_num,
                             progress_len=len(progress_content))
        else:
            _emit(session_id, "dev", "handoff",
                  message=f"세션 #{session_num} — 진행 파일 없음, 재시도")
            handoff_context = ""

    if not dev_complete:
        _emit(session_id, "dev", "max_sessions",
              message=f"최대 세션 수({MAX_SESSIONS})에 도달. 현재까지의 결과로 리포트를 생성합니다.")

    _emit(session_id, "report", "generating", message="완료 리포트 생성 중")

    report_system, report_user = build_report_prompt(
        task=task,
        folder_path=folder_path,
        backup_path=backup_path,
        report_dir=report_dir,
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
            cwd=folder_path,
            activity_event_type="upgrade_activity",
            emit_event_type="upgrade_progress",
        )
    except Exception as e:
        _logger.error("upgrade_report_error", error=str(e))

    from src.utils import report_renderer

    Path(report_dir).mkdir(parents=True, exist_ok=True)
    report_file = Path(report_dir) / "results.html"
    json_path = Path(report_dir) / "report.json"

    rendered = False
    if json_path.exists() and json_path.stat().st_size > 0:
        try:
            html = report_renderer.render_from_json_file(
                json_path,
                session_id=session_id,
                mode_label="Upgrade Report",
                fallback_title=task,
            )
            report_file.write_text(html, encoding="utf-8")
            rendered = True
            _logger.info("upgrade_rendered_from_json", path=str(report_file))
        except Exception as exc:
            _logger.warning("upgrade_render_json_failed", error=str(exc)[:200])

    if not rendered:
        sections = [
            {"heading": "원래 지시사항", "body_md": task[:2000]},
            {"heading": "작업한 앱 위치", "body_md": f"`{folder_path}`"},
            {"heading": "백업 위치", "body_md": f"`{backup_path}`"},
            {
                "heading": "롤백 방법",
                "body_md": (
                    "문제가 있으면 백업 폴더의 내용을 원본 위치로 복사해 복원하세요.\n\n"
                    f"```bash\ncp -R \"{backup_path}\"/* \"{folder_path}\"/\n```"
                ),
            },
        ]
        html = report_renderer.render_report(
            title="업그레이드 완료",
            sections=sections,
            mode_label="Upgrade Report",
            session_id=session_id,
            banner={
                "level": "warning",
                "title": "리포트 생성 단계가 끝나지 않아 기본 정보만 표시합니다",
                "body": "AI 가 최종 리포트를 작성하지 못해, 원본 지시사항과 폴더/백업 위치만 보여드립니다.",
            },
        )
        report_file.write_text(html, encoding="utf-8")
        _logger.info("upgrade_rendered_fallback", path=str(report_file))

    _emit(session_id, "report", "complete",
          report_path=f"/reports/{session_id}",
          folder_path=folder_path,
          backup_path=backup_path,
          message="리포트 생성 완료")

    return report_dir
