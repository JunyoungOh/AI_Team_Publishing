# src/upgrade/dev_runner.py
"""개발의뢰 — 0→1 최초개발 runner.

흐름:
  1. 명확화 질문 생성 (CLI 1회) → UI로 전달 → 사용자 답변 대기
  2. 개발 세션 실행 (CLI auto 모드, 장시간)
     - rate limit → 사용량 파일에서 리셋 시점 읽어 정확히 대기 후 재개
     - 컨텍스트 한계 → PROGRESS.md 기반 새 세션 handoff
  3. 완료 리포트 생성 (CLI 1회)

NOTE: 워크스페이스 네임스페이스는 역사적 이유로 'overtime'을 그대로 사용한다.
      (야근팀 탭에서 개발의뢰 탭으로 dev 모드가 이사할 때 디스크 경로를
       변경하지 않은 잔재 — 사용자 데이터 호환성을 위해 유지.)
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from src.config.settings import get_settings
from src.modes.common import emit_mode_event
from src.upgrade.dev_prompts import (
    build_clarify_prompt,
    build_dev_system_prompt,
    build_handoff_prompt,
    build_report_prompt,
)
from src.upgrade.dev_state import (
    DevState,
    GuardTriggeredError,
    check_guard,
    cleanup_session,
    get_lock,
    guard_remaining,
    wait_or_manual,
)
from src.utils.cli_session import (
    RateLimitError,
    _get_rate_limit_wait,
    _run_cli_session,
)
from src.utils.logging import get_logger
from src.utils.notifier import notify_completion

_logger = get_logger(agent_id="dev_runner")

_DEV_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "WebSearch", "WebFetch"]
_REPORT_TOOLS = ["Read", "Write", "Bash", "Glob", "Grep"]

MAX_SESSIONS = 10
_COMPLETION_MARKER = "ALL_PHASES_DONE"

# 개발의뢰이 사용하는 워크스페이스 네임스페이스 (역사적 이름).
_WORKSPACE_MODE = "overtime"


def _emit(session_id: str, phase: str, action: str, *, event_type: str = "dev_progress", **kwargs):
    """개발 모드 이벤트 emit 헬퍼. event_type은 강화소가 재사용할 수 있도록 파라미터화."""
    emit_mode_event(session_id, {
        "type": event_type,
        "data": {"phase": phase, "action": action, **kwargs},
    })


async def _run_with_state_retry(
    state: DevState,
    system_prompt: str,
    user_prompt: str,
    tools: list[str],
    phase: str,
    model: str = "sonnet",
    max_turns: int = 60,
    timeout: int = 420,
    cwd: str | None = None,
    activity_event_type: str = "overtime_activity",
    emit_event_type: str = "dev_progress",
    effort: str | None = None,
) -> str:
    """state.json 기반 CLI 실행 + 자동 재개.

    - 가드(6h/5회) 발동 시 GuardTriggeredError
    - rate limit: state.record_rate_limit으로 대기 시간 계산 + wait_or_manual
    - 수동 "지금 시도" 버튼은 같은 session_id의 asyncio.Event를 set해서
      timer를 조기 만료시킨다 (dev_state.trigger_manual_retry에서)
    """
    state.phase = phase

    def _on_success():
        state.record_success(time.time())
        state.save()

    while True:
        now_ts = time.time()

        if check_guard(state, now_ts):
            state.state = "stopped"
            state.error_reason = "guard_triggered"
            state.save()
            _emit(state.session_id, phase, "guard_triggered",
                  event_type=emit_event_type,
                  message="6시간 내 5회 재시도 실패 — 자동 중지. Claude 사용량 상태를 확인해주세요.")
            raise GuardTriggeredError()

        state.state = "running"
        state.save()

        try:
            return await _run_cli_session(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
                session_id=state.session_id,
                model=model,
                max_turns=max_turns,
                timeout=timeout,
                cwd=cwd,
                activity_event_type=activity_event_type,
                effort=effort,
                on_first_assistant=_on_success,
            )
        except RateLimitError:
            now_ts = time.time()
            wait_sec = state.record_rate_limit(now_ts)
            state.save()

            remaining = guard_remaining(state, now_ts)
            wait_min = max(1, wait_sec // 60)
            _emit(state.session_id, phase, "rate_limited",
                  event_type=emit_event_type,
                  wait_seconds=wait_sec,
                  next_retry_at=int(state.next_retry_at or 0),
                  retry_count=state.backoff_index,
                  guard_remaining=remaining,
                  message=f"사용량 한도 도달 — 약 {wait_min}분 후 자동 재개 (남은 시도 {remaining}회)")
            _logger.warning("dev_rate_limited",
                            phase=phase, session=state.session_id,
                            wait_s=wait_sec, retry=state.backoff_index)

            trigger = await wait_or_manual(state.session_id, wait_sec)
            _emit(state.session_id, phase, "retrying",
                  event_type=emit_event_type,
                  trigger=trigger,
                  message="재개 시도 중" + (" (수동 트리거)" if trigger == "manual" else ""))


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
    cwd: str | None = None,
    activity_event_type: str = "overtime_activity",
    emit_event_type: str = "dev_progress",
    effort: str | None = None,
) -> str:
    """CLI 세션 실행 + 사용량 파일 기반 rate limit 재시도.

    - rate limit: 사용량 파일에서 리셋 시점 읽어 대기 → 무한 재시도
    - 기타 오류: max_non_rl_retries까지만 재시도
    - cwd: 강화소 등 외부 폴더 작업 시 사용
    - emit_event_type: 재시도/rate-limit 알림을 어떤 WS 타입으로 보낼지
    - activity_event_type: 도구 사용 이벤트 WS 타입. 개발의뢰(최초개발)은
      mode-upgrade.js가 'overtime_activity'를 listen 하므로 기본값 유지
      (역사적 이름 — 야근팀 시절부터 쓰던 것). 강화소는 'upgrade_activity'
      를 별도로 전달한다.
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
                cwd=cwd,
                activity_event_type=activity_event_type,
                effort=effort,
            )
        except RateLimitError:
            wait_sec, is_rl = _get_rate_limit_wait()

            if not is_rl:
                non_rl_attempts += 1
                if non_rl_attempts >= max_non_rl_retries:
                    raise
                _emit(session_id, phase, "retry",
                      event_type=emit_event_type,
                      message=f"일시 오류 — 재시도 ({non_rl_attempts}/{max_non_rl_retries})")
                await asyncio.sleep(30)
                continue

            wait_min = wait_sec // 60
            _logger.warning("dev_rate_limited", phase=phase, wait_s=wait_sec)
            _emit(session_id, phase, "rate_limited",
                  event_type=emit_event_type,
                  message=f"사용량 한도 도달 — {wait_min}분 후 자동 재개",
                  cooldown=wait_sec,
                  resume_at=int(time.time()) + wait_sec)
            await asyncio.sleep(wait_sec)


async def generate_clarify_questions(
    task: str,
    session_id: str,
    workspace_files: list[str] | None = None,
) -> str:
    """명확화 질문 생성. bridge.structured_query + Pydantic 스키마로 구조화된 응답 강제.

    workspace_files가 있으면 `data/workspace/{_WORKSPACE_MODE}/input/` 기준
    절대경로로 변환해서 prompt에 주입한다. 이렇게 하면 clarify LLM이 파일
    선택 자체를 되묻지 않는다.
    """
    from pydantic import BaseModel, Field
    from src.utils.bridge_factory import get_bridge
    from src.utils.workspace import resolve_selected_paths

    class DevClarifyQuestions(BaseModel):
        """개발 모드 명확화 질문."""
        questions: list[str] = Field(min_length=1, max_length=5, description="3~5개 질문 목록")

    settings = get_settings()
    file_paths = resolve_selected_paths(_WORKSPACE_MODE, workspace_files or [])
    system, user = build_clarify_prompt(task, file_paths=file_paths)

    _emit(session_id, "clarify", "generating", message="명확화 질문 생성 중")

    bridge = get_bridge()
    # circuit breaker가 이전 작업 실패로 열려있을 수 있으므로 리셋
    bridge._circuit.record_success()

    last_error = None
    for attempt in range(2):
        try:
            # 인스턴트 모드 CEO.generate_all_questions와 동일 프로파일 사용:
            # effort="medium" + planning_timeout + ceo_max_turns
            # effort 명시 안 하면 Claude CLI가 extended thinking 기본값으로
            # 120s timeout을 초과한다 — 명시 필수
            result: DevClarifyQuestions = await bridge.structured_query(
                system_prompt=system,
                user_message=user,
                output_schema=DevClarifyQuestions,
                model=settings.worker_model,
                allowed_tools=[],
                timeout=settings.planning_timeout,
                max_turns=settings.ceo_max_turns,
                effort=settings.ceo_question_effort,
            )
            lines = [f"{i+1}. {q}" for i, q in enumerate(result.questions)]
            return "\n".join(lines)
        except Exception as e:
            last_error = e
            _logger.warning(
                "clarify_structured_error",
                attempt=attempt + 1,
                error_type=type(e).__name__,
                error=str(e)[:500],
            )
            if attempt == 0:
                await asyncio.sleep(2)

    _logger.error(
        "clarify_all_attempts_failed",
        error_type=type(last_error).__name__,
        error=str(last_error)[:500],
    )
    return "질문 생성에 실패했습니다. 자유롭게 설명을 추가해주세요."


async def run_dev_overtime(
    task: str,
    answers: str,
    session_id: str,
    user_id: str = "",
    overtime_id: str = "",
    workspace_files: list[str] | None = None,
) -> str:
    """개발 모드 메인 실행. 완료 시 report_dir 반환.

    workspace_files에 파일명이 담겨 있으면 `data/workspace/{_WORKSPACE_MODE}/input/`
    기준 절대경로로 변환해서 dev system prompt에 주입한다. CLI는 Read 도구로
    해당 경로의 파일을 직접 열어본다.

    state.json에 세션 메타데이터 + rate limit 추적을 영속화. 사용량 소진 시
    _run_with_state_retry 안에서 자동 대기 + 재개. 6h 내 5회 실패 시 가드.

    NOTE: `overtime_id` 인자는 더 이상 사용되지 않는다 (야근팀 storage 제거됨).
          하위 호환을 위해 시그니처는 유지하지만 무시된다.
    """
    del overtime_id  # unused — overtime storage CRUD 제거됨
    from src.utils.workspace import resolve_selected_paths

    settings = get_settings()
    work_dir = f"data/workspace/{_WORKSPACE_MODE}/output/{session_id}/app"
    report_dir = work_dir
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    file_paths = resolve_selected_paths(_WORKSPACE_MODE, workspace_files or [])

    # ── state.json 초기화: 새 세션이면 만들고, 혹시 존재하면 메타만 갱신 ──
    state_path = DevState.path_for(session_id, _WORKSPACE_MODE)
    if state_path.exists():
        try:
            state = DevState.load(state_path)
            # 이어서 시작: 진행 상황 보존, 상태는 running으로 리셋
            state.state = "running"
        except Exception as exc:
            _logger.warning("dev_state_load_failed", error=str(exc)[:200])
            state = DevState(session_id=session_id, user_id=user_id or "")
    else:
        state = DevState(session_id=session_id, user_id=user_id or "")
    state.task = task
    state.answers = answers
    state.workspace_files = workspace_files or []
    state.work_dir = work_dir
    state.save(state_path)

    started = time.time()

    # ── 세션 전체를 락으로 보호: 같은 session_id로 2중 실행 방지 ──
    session_lock = get_lock(session_id)
    async with session_lock:
        try:
            await _run_dev_inner(
                state, settings, file_paths, work_dir, report_dir, state_path,
            )
        except GuardTriggeredError:
            # 가드 발동 — state는 이미 stopped로 저장됨
            pass
        except asyncio.CancelledError:
            # 사용자 중지 버튼 → state를 stopped로
            state.state = "stopped"
            state.error_reason = "user_stopped"
            state.save(state_path)
            raise
        except Exception as exc:
            _logger.error("dev_run_fatal", error=str(exc)[:500])
            state.state = "error"
            state.error_reason = f"fatal: {type(exc).__name__}: {str(exc)[:200]}"
            state.save(state_path)
            raise
        finally:
            cleanup_session(session_id)

    # 사용자 취소 외 모든 종료 경로에 대해 알림 (done / stopped / error).
    # 취소는 위에서 raise로 빠져나가므로 여기 도달하지 않음.
    duration = round(time.time() - started, 2)
    task_title = (task or "개발의뢰").strip().splitlines()[0][:80]
    if state.state == "done":
        await notify_completion(
            kind="dev",
            title=task_title,
            summary="리포트 생성 완료",
            duration_seconds=duration,
            status="success",
        )
    elif state.state == "stopped":
        await notify_completion(
            kind="dev",
            title=task_title,
            summary=f"중단됨: {state.error_reason or '사용량 가드'}",
            duration_seconds=duration,
            status="failure",
        )
    elif state.state == "error":
        await notify_completion(
            kind="dev",
            title=task_title,
            summary=f"오류: {state.error_reason or '알 수 없음'}",
            duration_seconds=duration,
            status="failure",
        )

    return report_dir


async def _run_dev_inner(
    state: DevState,
    settings,
    file_paths: list[str],
    work_dir: str,
    report_dir: str,
    state_path: Path,
) -> None:
    """run_dev_overtime의 메인 바디. 락 안에서 실행. state 변경은 이 안에서 전부."""
    session_id = state.session_id
    task = state.task
    answers = state.answers
    handoff_context = state.handoff_context
    dev_complete = state.dev_complete

    # ── 개발 루프 (컨텍스트 한계 시 새 세션으로 이어받기) ──
    start_session = state.session_number + 1 if state.session_number else 1
    for session_num in range(start_session, MAX_SESSIONS + 1):
        _logger.info("dev_session_start", session=session_num, session_id=session_id)
        state.session_number = session_num
        state.phase = "dev"
        state.save(state_path)

        system_prompt = build_dev_system_prompt(
            task=task,
            answers=answers,
            work_dir=work_dir,
            handoff_context=handoff_context,
            file_paths=file_paths,
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

        try:
            await _run_with_state_retry(
                state=state,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=_DEV_TOOLS,
                phase="dev",
                model=settings.worker_model,
                max_turns=200,
                timeout=1800,
                effort=settings.worker_effort,
            )
        except GuardTriggeredError:
            raise  # 상위에서 잡아서 state=stopped 처리 완료됨
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
                state.dev_complete = True
                state.save(state_path)
                _emit(session_id, "dev", "complete",
                      message="개발 완료 — 리포트 생성 중")
                break
            else:
                _emit(session_id, "dev", "handoff",
                      message=f"세션 #{session_num} 종료 — 새 세션으로 이어받기")
                handoff_context = progress_content[-3000:]
                state.handoff_context = handoff_context
                state.save(state_path)
                _logger.info("dev_handoff", session=session_num,
                             progress_len=len(progress_content))
        else:
            _emit(session_id, "dev", "handoff",
                  message=f"세션 #{session_num} — 진행 파일 없음, 재시도")
            handoff_context = ""
            state.handoff_context = ""
            state.save(state_path)

    if not dev_complete:
        _emit(session_id, "dev", "max_sessions",
              message=f"최대 세션 수({MAX_SESSIONS})에 도달. 현재까지의 결과로 리포트를 생성합니다.")

    # ── 완료 리포트 생성 ──
    state.phase = "report"
    state.save(state_path)
    _emit(session_id, "report", "generating",
          message="앱 설명 리포트 + 실행 가이드 생성 중")

    report_system, report_user = build_report_prompt(
        task=task, work_dir=work_dir, report_dir=report_dir,
    )

    try:
        await _run_with_state_retry(
            state=state,
            system_prompt=report_system,
            user_prompt=report_user,
            tools=_REPORT_TOOLS,
            phase="report",
            model=settings.worker_model,
            max_turns=20,
            timeout=300,
            effort=settings.worker_effort,
        )
    except GuardTriggeredError:
        raise
    except Exception as e:
        _logger.error("dev_report_error", error=str(e))

    # 리포트 렌더링: report.json 우선, 없으면 파일 목록 fallback
    from src.utils import report_renderer

    Path(report_dir).mkdir(parents=True, exist_ok=True)
    report_file = Path(report_dir) / "guide.html"
    json_path = Path(report_dir) / "report.json"

    rendered = False
    if json_path.exists() and json_path.stat().st_size > 0:
        try:
            html = report_renderer.render_from_json_file(
                json_path,
                session_id=session_id,
                mode_label="Development Report",
                fallback_title=task,
            )
            report_file.write_text(html, encoding="utf-8")
            rendered = True
            _logger.info("dev_rendered_from_json", path=str(report_file))
        except Exception as exc:
            _logger.warning("dev_render_json_failed", error=str(exc)[:200])

    if not rendered:
        files = sorted(
            f.relative_to(work_dir) for f in Path(work_dir).rglob("*") if f.is_file()
        )
        file_list_md = "\n".join(f"- `{f}`" for f in files) or "_파일 없음_"
        sections = [
            {"heading": "원래 요청", "body_md": task[:2000]},
            {"heading": "생성된 파일", "body_md": file_list_md},
            {
                "heading": "실행 방법",
                "body_md": (
                    f"앱 폴더로 이동한 뒤 실행하세요.\n\n"
                    f"```bash\ncd {work_dir}\n```"
                ),
            },
        ]
        html = report_renderer.render_report(
            title="개발 완료",
            sections=sections,
            mode_label="Development Report",
            session_id=session_id,
            banner={
                "level": "warning",
                "title": "리포트 생성 단계가 끝나지 않아 파일 목록만 표시합니다",
                "body": "AI 가 최종 리포트를 작성하지 못해, 생성된 파일 목록과 기본 실행 안내만 보여드립니다.",
            },
        )
        report_file.write_text(html, encoding="utf-8")
        _logger.info("dev_rendered_fallback", path=str(report_file))

    # ── 더블클릭 실행용 run.command 작성 ──
    try:
        from src.utils.run_command_writer import write_run_command

        run_cmd_path = write_run_command(work_dir)
    except Exception as exc:
        _logger.warning("dev_run_command_failed", error=str(exc)[:200])
        run_cmd_path = None

    if run_cmd_path is not None:
        _emit(session_id, "report", "run_command_ready",
              path=str(run_cmd_path),
              message=f"더블클릭 실행 파일 생성: {run_cmd_path.name}")

    _emit(session_id, "report", "complete",
          report_path=f"/apps/{session_id}/guide",
          app_dir=work_dir,
          run_command=str(run_cmd_path) if run_cmd_path else None,
          message="리포트 생성 완료")

    # 최종 상태 저장 — 재접속 시 이 세션은 '완료됨'으로 표시됨
    state.state = "done"
    state.save(state_path)
