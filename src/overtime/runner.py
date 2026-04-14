"""야근팀 실행 엔진 — 목표 달성까지 반복 iteration.

각 iteration:
  1. 싱글 세션으로 정보 수집 → raw_{n}.md 저장
  2. 평가 세션으로 달성률 판단
  3. score >= 90 이면 최종 보고서 생성, 아니면 다음 iteration

파일 시스템이 iteration 간 메모리 역할.
NOTE: asyncio.create_subprocess_exec 사용하여 shell injection 방지.
      모든 CLI 인자는 배열로 전달됨 (shell=False).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from src.config.settings import get_settings
from src.modes.common import emit_mode_event
from src.overtime.prompts import (
    build_evaluation_prompt,
    build_final_report_prompt,
    build_iteration_prompt,
)
from src.utils.logging import get_logger

_logger = get_logger(agent_id="overtime_runner")

_OVERTIME_TOOLS = [
    "WebSearch", "WebFetch", "Read", "Write",
    "Bash", "Glob", "Grep", "Agent",
    "mcp__firecrawl__firecrawl_scrape",
]

_EVAL_TOOLS = ["Read", "Glob"]

SCORE_THRESHOLD = 90

# rate limit 감지 키워드
_RATE_LIMIT_SIGNALS = ["rate_limit", "rate limit", "overloaded", "429", "quota"]

_USAGE_FILE = Path("/tmp/claude-usage.json")


class RateLimitError(Exception):
    """CLI 세션이 rate limit에 걸렸을 때 발생."""
    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


def _get_rate_limit_wait() -> tuple[int, bool]:
    """사용량 파일에서 대기 시간 계산.

    Returns:
        (wait_seconds, is_rate_limit):
        - is_rate_limit=True: 실제 rate limit. wait_seconds만큼 대기 후 재개
        - is_rate_limit=False: rate limit이 아닌 다른 오류
    """
    if not _USAGE_FILE.exists():
        return 300, False

    try:
        data = json.loads(_USAGE_FILE.read_text())
    except Exception:
        return 300, False

    five_hour = data.get("five_hour") or {}
    used_pct = five_hour.get("used_percentage", 0)
    resets_at = five_hour.get("resets_at")

    if used_pct < 80:
        return 0, False

    if resets_at:
        wait = max(int(resets_at - time.time()) + 30, 60)
        return min(wait, 7200), True

    return 300, True


def _tool_detail(tool_name: str, inp: dict) -> str:
    """도구 input에서 핵심 정보 한 줄 추출. 자동개발 탭 활동 카드에 표시.

    너무 길면 잘라서 UI 한 줄에 맞게 반환. 빈 문자열이면 UI는 detail 없이
    기존 "도구명 × 카운트" 형태로만 표시 (기존 동작 유지).
    """
    if not isinstance(inp, dict):
        return ""

    if tool_name == "WebSearch":
        q = inp.get("query") or ""
        return f"'{q[:60]}'" if q else ""
    if tool_name == "WebFetch":
        url = inp.get("url") or ""
        return url if len(url) <= 60 else url[:57] + "..."
    if tool_name in ("Read", "Write", "Edit"):
        path = inp.get("file_path") or ""
        if not path:
            return ""
        # 마지막 2 경로 요소만 (src/main.py, components/Button.tsx 등)
        parts = path.rsplit("/", 2)
        return "/".join(parts[-2:]) if len(parts) > 1 else path
    if tool_name == "Bash":
        cmd = inp.get("command") or ""
        return cmd if len(cmd) <= 70 else cmd[:67] + "..."
    if tool_name in ("Glob", "Grep"):
        pattern = inp.get("pattern") or inp.get("query") or ""
        return pattern if len(pattern) <= 60 else pattern[:57] + "..."
    if tool_name == "Agent":
        desc = inp.get("description") or inp.get("prompt") or ""
        desc = str(desc)
        return desc if len(desc) <= 80 else desc[:77] + "..."
    return ""


async def _run_cli_session(
    system_prompt: str,
    user_prompt: str,
    tools: list[str],
    session_id: str,
    model: str = "sonnet",
    max_turns: int = 60,
    timeout: int = 420,
    cwd: str | None = None,
    activity_event_type: str = "overtime_activity",
    effort: str | None = None,
) -> str:
    """CLI subprocess를 실행하고 결과를 반환. rate limit 시 RateLimitError.

    cwd: 기본 None(프로젝트 루트). 강화소처럼 사용자 폴더 안에서 실행해야 할 때 지정.
    activity_event_type: 도구 사용 이벤트 WS 타입. 강화소는 "upgrade_activity" 전달.
    """
    from src.utils.claude_code import (
        _register_process,
        _unregister_process,
        _kill_process_tree,
    )

    # asyncio.create_subprocess_exec: 인자가 배열로 전달되어 shell injection 방지
    cmd = [
        "claude", "-p", user_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--max-turns", str(max_turns),
        "--append-system-prompt", system_prompt,
        "--allowedTools", ",".join(tools),
        "--permission-mode", "auto",
    ]
    if effort:
        cmd.extend(["--effort", effort])

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or os.getcwd(),
        start_new_session=True,
        env=env,
        limit=sys.maxsize,
    )
    _register_process(proc)

    full_text = ""
    tool_count = 0
    start = time.time()

    _TOOL_LABELS = {
        "WebSearch": "검색", "WebFetch": "수집",
        "Agent": "에이전트", "Write": "저장",
        "Read": "읽기", "Bash": "실행",
        "Edit": "편집", "Glob": "파일 검색", "Grep": "코드 검색",
    }

    try:
        async with asyncio.timeout(timeout):
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event.get("type") == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            full_text += block["text"]
                        elif block.get("type") == "tool_use":
                            tool_count += 1
                            tool_name = block.get("name", "")
                            label = _TOOL_LABELS.get(tool_name, tool_name)
                            detail = _tool_detail(tool_name, block.get("input") or {})
                            emit_mode_event(session_id, {
                                "type": activity_event_type,
                                "data": {
                                    "tool": tool_name,
                                    "label": label,
                                    "count": tool_count,
                                    "detail": detail,
                                },
                            })

                elif event.get("type") == "result":
                    result_text = event.get("result", "")
                    is_error = event.get("is_error", False)
                    subtype = event.get("subtype", "")

                    # rate limit 감지
                    if is_error:
                        error_lower = (result_text + subtype).lower()
                        for signal in _RATE_LIMIT_SIGNALS:
                            if signal in error_lower:
                                _logger.warning("overtime_rate_limited", result=result_text[:200])
                                raise RateLimitError(result_text[:200])

                    if not full_text and result_text:
                        full_text = result_text

    except TimeoutError:
        _logger.warning("overtime_session_timeout", elapsed=round(time.time() - start, 1))
        await _kill_process_tree(proc)
    except RateLimitError:
        await _kill_process_tree(proc)
        raise  # 상위에서 처리
    finally:
        _unregister_process(proc)

    return full_text


def _parse_eval_json(text: str) -> dict:
    """평가 결과에서 eval_json 블록을 추출."""
    marker = "```eval_json"
    idx = text.find(marker)
    if idx == -1:
        return {"score": 0, "summary": "평가 파싱 실패", "gaps": [], "recommendation": ""}
    start = idx + len(marker)
    end = text.find("```", start)
    if end == -1:
        return {"score": 0, "summary": "평가 파싱 실패", "gaps": [], "recommendation": ""}
    try:
        return json.loads(text[start:end].strip())
    except json.JSONDecodeError:
        return {"score": 0, "summary": "평가 JSON 파싱 실패", "gaps": [], "recommendation": ""}


async def run_overtime(
    task: str,
    strategy: dict | None,
    goal: str,
    session_id: str,
    user_id: str = "",
    max_iterations: int = 5,
    overtime_id: str = "",
    file_context: str = "",  # NEW
) -> str:
    """야근팀 전체 루프 실행. 완료 시 report_dir 반환."""
    from src.company_builder.storage import update_overtime_iteration

    from src.utils.report_paths import build_report_dir

    settings = get_settings()
    work_dir = f"data/overtime/{session_id}"
    report_dir = str(build_report_dir(task, session_id=session_id))
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    previous_eval = None
    iteration = 0

    effective_task = task
    if file_context:
        effective_task = task + "\n\n" + file_context

    for iteration in range(1, max_iterations + 1):
        _logger.info("overtime_iteration_start", iteration=iteration, session_id=session_id)

        emit_mode_event(session_id, {
            "type": "overtime_iteration",
            "data": {
                "action": "start",
                "iteration": iteration,
                "max_iterations": max_iterations,
            },
        })

        # 1. 수집 세션 (rate limit 시 리셋까지 대기 후 자동 재개)
        system, user = build_iteration_prompt(
            task=effective_task, strategy=strategy, goal=goal,
            iteration=iteration, work_dir=work_dir,
            previous_eval=previous_eval,
        )

        iter_start = time.time()
        non_rl_attempts = 0
        while True:
            try:
                await _run_cli_session(
                    system_prompt=system, user_prompt=user,
                    tools=_OVERTIME_TOOLS, session_id=session_id,
                    model=settings.worker_model, max_turns=60, timeout=420,
                    effort=settings.worker_effort,
                )
                break
            except RateLimitError:
                wait_sec, is_rl = _get_rate_limit_wait()
                if not is_rl:
                    non_rl_attempts += 1
                    if non_rl_attempts >= 3:
                        break
                    emit_mode_event(session_id, {
                        "type": "overtime_iteration",
                        "data": {"action": "retry", "iteration": iteration,
                                 "message": f"일시 오류 — 재시도 ({non_rl_attempts}/3)"},
                    })
                    await asyncio.sleep(30)
                    continue
                wait_min = wait_sec // 60
                _logger.warning("overtime_rate_limit_hit", iteration=iteration, wait_s=wait_sec)
                emit_mode_event(session_id, {
                    "type": "overtime_iteration",
                    "data": {
                        "action": "rate_limited",
                        "iteration": iteration,
                        "message": f"⏸️ 사용량 한도 도달 — {wait_min}분 후 자동 재개",
                        "cooldown": wait_sec,
                        "resume_at": int(time.time()) + wait_sec,
                    },
                })
                if overtime_id and user_id:
                    update_overtime_iteration(user_id, overtime_id, {
                        "id": f"{iteration}_pause",
                        "action": "rate_limited",
                        "cooldown_s": wait_sec,
                    }, status="paused")
                await asyncio.sleep(wait_sec)
        iter_elapsed = round(time.time() - iter_start, 1)

        # 2. 평가 세션 (rate limit 시 리셋까지 대기)
        eval_system, eval_user = build_evaluation_prompt(work_dir, goal, iteration)
        while True:
            try:
                eval_text = await _run_cli_session(
                    system_prompt=eval_system, user_prompt=eval_user,
                    tools=_EVAL_TOOLS, session_id=session_id,
                    model=settings.worker_model, max_turns=10, timeout=120,
                    effort=settings.worker_effort,
                )
                break
            except RateLimitError:
                wait_sec, is_rl = _get_rate_limit_wait()
                if not is_rl:
                    eval_text = ""
                    break
                _logger.warning("overtime_eval_rate_limited", iteration=iteration, wait_s=wait_sec)
                emit_mode_event(session_id, {
                    "type": "overtime_iteration",
                    "data": {"action": "rate_limited",
                             "message": f"⏸️ 평가 중 한도 도달 — {wait_sec // 60}분 후 자동 재개",
                             "cooldown": wait_sec,
                             "resume_at": int(time.time()) + wait_sec},
                })
                await asyncio.sleep(wait_sec)

        eval_result = _parse_eval_json(eval_text)
        score = eval_result.get("score", 0)
        previous_eval = eval_result

        _logger.info("overtime_iteration_complete", iteration=iteration, score=score, elapsed_s=iter_elapsed)

        iter_record = {
            "id": iteration,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_s": iter_elapsed,
            "score": score,
            "summary": eval_result.get("summary", ""),
            "gaps": eval_result.get("gaps", []),
        }

        status = "running"
        if score >= SCORE_THRESHOLD or iteration >= max_iterations:
            status = "finalizing"

        if overtime_id and user_id:
            update_overtime_iteration(user_id, overtime_id, iter_record, status)

        emit_mode_event(session_id, {
            "type": "overtime_iteration",
            "data": {
                "action": "scored",
                "iteration": iteration,
                "score": score,
                "summary": eval_result.get("summary", ""),
                "gaps": eval_result.get("gaps", []),
                "elapsed": iter_elapsed,
            },
        })

        if score >= SCORE_THRESHOLD:
            _logger.info("overtime_goal_reached", iteration=iteration, score=score)
            break

    # 3. 최종 보고서 생성
    emit_mode_event(session_id, {
        "type": "overtime_iteration",
        "data": {"action": "finalizing", "message": "최종 보고서 생성 중..."},
    })

    final_system, final_user = build_final_report_prompt(task, work_dir, report_dir)
    non_rl_attempts = 0
    while True:
        try:
            await _run_cli_session(
                system_prompt=final_system, user_prompt=final_user,
                tools=_OVERTIME_TOOLS, session_id=session_id,
                model=settings.worker_model, max_turns=40, timeout=300,
                effort=settings.worker_effort,
            )
            break
        except RateLimitError:
            wait_sec, is_rl = _get_rate_limit_wait()
            if not is_rl:
                non_rl_attempts += 1
                if non_rl_attempts >= 3:
                    break
                await asyncio.sleep(30)
                continue
            _logger.warning("overtime_final_report_rate_limited", wait_s=wait_sec)
            emit_mode_event(session_id, {
                "type": "overtime_iteration",
                "data": {"action": "rate_limited",
                         "message": f"⏸️ 보고서 생성 중 한도 도달 — {wait_sec // 60}분 후 자동 재개",
                         "cooldown": wait_sec,
                         "resume_at": int(time.time()) + wait_sec},
            })
            await asyncio.sleep(wait_sec)

    # 결과 렌더링: CLI 가 만든 report.json → 프로페셔널 HTML 로 변환.
    # 실패하면 raw_*.md 통합본을 마크다운 fallback 으로 같은 템플릿에 렌더.
    from src.utils import report_renderer

    Path(report_dir).mkdir(parents=True, exist_ok=True)
    report_path = Path(report_dir) / "results.html"
    json_path = Path(report_dir) / "report.json"

    rendered = False
    if json_path.exists() and json_path.stat().st_size > 0:
        try:
            html = report_renderer.render_from_json_file(
                json_path,
                session_id=session_id,
                mode_label="Overtime Report",
                fallback_title=task,
            )
            report_path.write_text(html, encoding="utf-8")
            rendered = True
            _logger.info("overtime_rendered_from_json", path=str(report_path))
        except Exception as exc:
            _logger.warning("overtime_render_json_failed", error=str(exc)[:200])

    if not rendered:
        raw_files = sorted(Path(work_dir).glob("raw_*.md"))
        combined = "\n\n---\n\n".join(
            f.read_text(encoding="utf-8") for f in raw_files if f.exists()
        )
        if combined.strip():
            sections = [{"heading": "수집된 리서치", "body_md": combined}]
            html = report_renderer.render_report(
                title=task,
                sections=sections,
                mode_label="Overtime Report",
                session_id=session_id,
                banner={
                    "level": "warning",
                    "title": "구조화 보고서 생성 실패 — 원본 수집 자료를 그대로 표시합니다",
                    "body": "AI 가 최종 정리 단계에서 보고서를 만들지 못해, 각 iteration 에서 수집된 원본 자료를 그대로 보여드립니다.",
                },
            )
            report_path.write_text(html, encoding="utf-8")
            _logger.info("overtime_rendered_from_raw_md", path=str(report_path))
        else:
            html = report_renderer.render_partial_fallback(
                user_task=task,
                session_id=session_id,
                raw_text="",
                reason="empty_result",
                mode_label="Overtime Report",
            )
            report_path.write_text(html, encoding="utf-8")
            _logger.warning("overtime_empty_fallback", path=str(report_path))

    if overtime_id and user_id:
        update_overtime_iteration(
            user_id, overtime_id,
            {"id": "final", "action": "report_generated", "report_dir": report_dir},
            status="completed",
        )

    emit_mode_event(session_id, {
        "type": "overtime_iteration",
        "data": {
            "action": "completed",
            "report_path": f"/reports/{session_id}",
            "total_iterations": iteration,
        },
    })

    return report_dir
