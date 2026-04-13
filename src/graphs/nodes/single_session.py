"""Single CLI session execution node — streaming version.

명확화 질문 완료 후, 하나의 Claude Code CLI 세션에서
리서치 → 합성 → HTML 보고서 생성까지 전체 작업을 실행한다.

Secretary chat_engine.py의 스트리밍 패턴을 적용:
- subprocess stdout을 line-by-line으로 읽어 stream-json 파싱
- tool_use 이벤트를 mode event queue로 실시간 전달
- sim_runner가 queue를 폴링하여 WebSocket으로 브라우저에 전송

NOTE: asyncio.create_subprocess_exec를 사용하여 shell injection을 방지.
      모든 인자는 배열로 전달됨 (shell=False).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from langchain_core.messages import AIMessage

from src.config.settings import get_settings
from src.modes.common import emit_mode_event
from src.prompts.single_session_prompts import (
    SINGLE_SESSION_SYSTEM,
    build_execution_prompt,
)
from src.utils.logging import get_logger
from src.utils.streaming_cards import (
    CardEmitter,
    handle_assistant_block,
    handle_user_block,
    heartbeat_loop,
)

_logger = get_logger(agent_id="single_session")

# 싱글 세션에서 사용할 도구 목록
_SESSION_TOOLS = [
    "WebSearch", "WebFetch", "Read", "Write",
    "Bash", "Glob", "Grep", "Agent",
    "mcp__firecrawl__firecrawl_scrape",
]

_MCP_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _build_runtime_mcp_config() -> tuple[str | None, frozenset[str]]:
    """프로젝트 .mcp.json을 읽어 env 치환 후 임시 파일에 저장.

    Claude Code CLI의 ``--mcp-config`` 플래그로 넘길 JSON을 런타임에 생성한다.
    ``~/.claude.json``의 trust/enable 상태와 무관하게 MCP 서버를 기동시켜,
    ``.env``에 키만 있으면 동작하는 배포 친화적 경로를 확보한다.

    env 값이 비어 있는 서버는 config에서 제거한다 (빈 키로 기동하면
    firecrawl-mcp 같은 서버가 즉시 종료되어 연쇄 실패를 유발하기 때문).

    Returns:
        (temp_file_path, enabled_server_names).
        .mcp.json이 없거나 활성 서버가 없으면 (None, frozenset()).
    """
    template = Path(".mcp.json")
    if not template.exists():
        return None, frozenset()

    try:
        raw = template.read_text(encoding="utf-8")
        substituted = _MCP_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""),
            raw,
        )
        config = json.loads(substituted)
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("mcp_config_template_load_failed", error=str(exc))
        return None, frozenset()

    servers = config.get("mcpServers") or {}
    pruned: dict[str, dict] = {}
    for name, cfg in servers.items():
        env_map = cfg.get("env") or {}
        if env_map and not all(env_map.values()):
            _logger.info("mcp_server_skipped_missing_env", server=name)
            continue
        pruned[name] = cfg

    if not pruned:
        return None, frozenset()

    config["mcpServers"] = pruned

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        prefix="single_session_",
        suffix="_mcp.json",
        delete=False,
        encoding="utf-8",
    )
    try:
        json.dump(config, tmp)
        tmp.flush()
    finally:
        tmp.close()

    return tmp.name, frozenset(pruned.keys())


def _filter_session_tools(tools: list[str], enabled_mcp: frozenset[str]) -> list[str]:
    """활성 MCP 서버에 속한 도구만 남긴다.

    ``mcp__<server>__<tool>`` 형식은 <server>가 enabled_mcp에 포함될 때만 유지.
    non-MCP 도구(WebSearch, Read 등)는 전부 유지한다.
    """
    filtered: list[str] = []
    for tool in tools:
        if not tool.startswith("mcp__"):
            filtered.append(tool)
            continue
        parts = tool.split("__", 2)
        if len(parts) < 3:
            continue
        server_name = parts[1]
        if server_name in enabled_mcp:
            filtered.append(tool)
        else:
            _logger.info(
                "session_tool_dropped_mcp_disabled",
                tool=tool,
                server=server_name,
            )
    return filtered


def _build_report_dir(user_task: str, session_id: str) -> str:
    """task 제목을 기반으로 보고서 폴더 경로를 생성."""
    # 제목에서 폴더명 생성 (최대 50자, 파일시스템 안전 문자만)
    name = user_task.strip()[:50]
    # 파일시스템에 안전하지 않은 문자 제거
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name).strip('_')
    if not name:
        name = session_id
    # 동일 이름 충돌 방지: 이미 존재하면 session_id 접미사
    base = f"data/reports/{name}"
    if Path(base).exists():
        base = f"data/reports/{name}_{session_id[:6]}"
    return base


def _extract_qa_context(state: dict) -> tuple[list[str], list[str]]:
    """state에서 명확화 질문과 사용자 답변을 추출."""
    questions = []
    answers = state.get("user_answers", [])

    raw_q = state.get("clarifying_questions", [])
    if isinstance(raw_q, list):
        for item in raw_q:
            if isinstance(item, dict):
                questions.append(item.get("question_text", str(item)))
            elif isinstance(item, str):
                questions.append(item)
    elif isinstance(raw_q, str):
        questions = [raw_q]

    return questions, answers


async def _stream_session(
    prompt: str,
    system_prompt: str,
    session_id: str,
    model: str,
    max_turns: int,
    timeout: int,
) -> tuple[str, bool]:
    """CLI subprocess를 스트리밍으로 실행하고 활동 이벤트를 emit.

    Returns:
        (full_text, timed_out) — timed_out이 True면 타임아웃으로 중단됨.
    """
    from src.utils.claude_code import (
        _register_process,
        _unregister_process,
        _kill_process_tree,
        set_session_tag,
    )

    set_session_tag(f"single_{session_id}")

    # 런타임 MCP 설정 주입: .mcp.json을 env 치환 후 임시 파일로 써서 --mcp-config로 전달.
    # ~/.claude.json의 trust/enable 상태와 무관하게 MCP 서버가 기동되도록 하여
    # 배포 환경에서도 .env에 키만 넣으면 동작하는 패턴을 복원한다.
    mcp_config_path, enabled_mcp = _build_runtime_mcp_config()
    session_tools = _filter_session_tools(_SESSION_TOOLS, enabled_mcp)

    # asyncio.create_subprocess_exec: 인자가 배열로 전달되어 shell injection 방지
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--max-turns", str(max_turns),
        "--append-system-prompt", system_prompt,
        "--allowedTools", ",".join(session_tools),
        "--permission-mode", "auto",
    ]
    if mcp_config_path:
        cmd.extend(["--mcp-config", mcp_config_path, "--strict-mcp-config"])

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    cwd = os.getcwd()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
        env=env,
        limit=sys.maxsize,
    )
    _register_process(proc)

    # 스트리밍 카드 헬퍼 상태 — 한 세션에 emitter 하나, 끝까지 재사용
    emitter = CardEmitter.from_session_id(session_id)
    text_accumulator: list[str] = []
    tool_count_ref: list[int] = [0]
    tool_use_map: dict[str, str] = {}

    timed_out = False
    start_time = time.time()

    # 시작 이벤트
    emit_mode_event(session_id, {
        "type": "activity",
        "data": {
            "action": "started",
            "message": "🚀 AI 세션이 시작되었습니다",
            "elapsed": 0,
        },
    })

    # 침묵 감지 하트비트 — 15초 이상 이벤트가 없으면 주기적으로 "작업 중" 카드 갱신.
    # Write같이 큰 content를 inline으로 담는 도구 호출 대기 구간을 메움.
    heartbeat_task = asyncio.create_task(heartbeat_loop(emitter, start_time=start_time))

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

                event_type = event.get("type")
                elapsed = round(time.time() - start_time, 1)

                if event_type == "assistant":
                    message = event.get("message", {})
                    for block in message.get("content", []):
                        if not isinstance(block, dict):
                            continue
                        await handle_assistant_block(
                            block,
                            emitter=emitter,
                            elapsed=elapsed,
                            text_accumulator=text_accumulator,
                            tool_count_ref=tool_count_ref,
                            tool_use_map=tool_use_map,
                        )

                elif event_type == "user":
                    message = event.get("message", {})
                    for block in message.get("content", []):
                        if not isinstance(block, dict):
                            continue
                        await handle_user_block(
                            block,
                            emitter=emitter,
                            elapsed=elapsed,
                            tool_use_map=tool_use_map,
                        )

                elif event_type == "result":
                    result_text = event.get("result", "")
                    if event.get("is_error"):
                        _logger.warning("single_session_result_error", error=result_text[:200])
                        if not text_accumulator:
                            text_accumulator.append(result_text)
                    elif not text_accumulator and result_text:
                        text_accumulator.append(result_text)

    except TimeoutError:
        timed_out = True
        elapsed = round(time.time() - start_time, 1)
        _logger.warning("single_session_timeout", elapsed_s=elapsed, timeout=timeout)
        emit_mode_event(session_id, {
            "type": "activity",
            "data": {
                "action": "timeout",
                "message": f"⏱️ 타임아웃 ({elapsed}초) — 부분 결과를 저장합니다",
                "elapsed": elapsed,
            },
        })
        await _kill_process_tree(proc)
    finally:
        # 하트비트 task 정리 — stream loop이 끝나면 더는 필요 없음
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        _unregister_process(proc)
        if mcp_config_path:
            try:
                os.unlink(mcp_config_path)
            except OSError:
                pass

    full_text = "".join(text_accumulator)
    tool_count = tool_count_ref[0]
    elapsed = round(time.time() - start_time, 1)
    completed_msg = (
        f"⚠️ 작업이 시간 제한 ({timeout}초)으로 중단되었습니다 — 부분 결과만 저장됨"
        if timed_out
        else f"✅ 작업 완료 ({elapsed}초, 도구 {tool_count}회 사용)"
    )
    emit_mode_event(session_id, {
        "type": "activity",
        "data": {
            "action": "completed",
            "message": completed_msg,
            "elapsed": elapsed,
            "tool_count": tool_count,
            "timed_out": timed_out,
        },
    })

    return full_text, timed_out


async def single_session_node(state: dict) -> dict:
    """싱글 CLI 세션으로 전체 작업을 실행하고 HTML 보고서를 생성한다.

    async 노드 — PipelineEngine._call_node이 iscoroutinefunction 감지하여
    await로 호출. 메인 이벤트 루프를 블로킹하지 않아 mode event drain이
    동시에 동작하여 활동 이벤트를 실시간 WebSocket 전송 가능.
    """
    if state.get("phase") == "error":
        return {}

    settings = get_settings()
    session_id = state.get("session_id", "default")
    user_task = state.get("user_task", "")

    # 폴더명: task 제목 기반 (안전한 파일명으로 변환)
    report_dir = _build_report_dir(user_task, session_id)
    questions, answers = _extract_qa_context(state)
    domains = state.get("selected_domains", ["research"])
    complexity = state.get("estimated_complexity", "low")

    # 전략 프리셋 로드
    strategy = None
    pre_context = state.get("pre_context") or {}
    # 1) pre_context에 strategy 객체가 직접 전달된 경우 (UI에서 전략 실행)
    if pre_context.get("strategy"):
        strategy = pre_context["strategy"]
        _logger.info("single_session_strategy_direct", strategy=strategy.get("name", ""))
    # 2) strategy_id로 storage에서 로드하는 경우
    elif pre_context.get("strategy_id"):
        from src.company_builder.storage import load_strategy
        user_id = state.get("user_id", "")
        strategy = load_strategy(user_id, pre_context["strategy_id"])
        if strategy:
            _logger.info("single_session_strategy_loaded", strategy=strategy.get("name", ""))

    _logger.info(
        "single_session_start",
        session_id=session_id,
        task=user_task[:80],
        domains=domains,
        complexity=complexity,
        strategy=strategy.get("name") if strategy else None,
    )

    # 출력 형식: UI 선택(pre_context.output_format)이 항상 최우선
    output_format = pre_context.get("output_format", "html")

    # Delta 비교 / Append 모드
    previous_report_path = pre_context.get("previous_report_path")
    output_mode = pre_context.get("output_mode", "replace")
    is_scheduled = state.get("execution_mode") == "scheduled"

    # 스케줄/Append 모드: 기존 보고서와 같은 디렉터리에 출력
    if previous_report_path:
        existing_dir = str(Path(previous_report_path).parent)
        if Path(existing_dir).exists():
            report_dir = existing_dir

    prompt = build_execution_prompt(
        user_task=user_task,
        user_answers=answers,
        clarifying_questions=questions,
        domains=domains,
        complexity=complexity,
        report_dir=report_dir,
        strategy=strategy,
        output_format=output_format,
        previous_report_path=previous_report_path,
        output_mode=output_mode,
        is_scheduled=is_scheduled,
    )

    # 전략 복잡도에 따른 타임아웃 조정:
    # - 5+ 관점 병렬 리서치 → high
    # - 3-4 관점 → 최소 medium
    # - settings.single_session_timeout을 기준점으로 승수 적용
    effective_complexity = complexity
    if strategy:
        num_perspectives = len(strategy.get("perspectives", []))
        if num_perspectives >= 5:
            effective_complexity = "high"
        elif num_perspectives >= 3 and effective_complexity == "low":
            effective_complexity = "medium"

    base_timeout = max(settings.single_session_timeout, 600)  # 최소 10분 안전장치
    base_max_turns = max(settings.single_session_max_turns, 60)

    timeout_multiplier = {"high": 1.5, "medium": 1.0, "low": 0.7}
    turns_multiplier = {"high": 1.5, "medium": 1.0, "low": 0.75}
    timeout = int(base_timeout * timeout_multiplier.get(effective_complexity, 1.0))
    max_turns = int(base_max_turns * turns_multiplier.get(effective_complexity, 1.0))

    _logger.info(
        "single_session_timeout_config",
        session_id=session_id,
        complexity=effective_complexity,
        timeout=timeout,
        max_turns=max_turns,
        perspectives=len(strategy.get("perspectives", [])) if strategy else 0,
        base_timeout=base_timeout,
    )

    start_time = time.time()
    timed_out = False

    try:
        result, timed_out = await _stream_session(
            prompt=prompt,
            system_prompt=SINGLE_SESSION_SYSTEM,
            session_id=session_id,
            model=settings.worker_model,
            max_turns=max_turns,
            timeout=timeout,
        )
        elapsed = time.time() - start_time
        _logger.info(
            "single_session_complete",
            session_id=session_id,
            elapsed_s=round(elapsed, 1),
            result_len=len(result),
            timed_out=timed_out,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        _logger.error(
            "single_session_failed",
            session_id=session_id,
            elapsed_s=round(elapsed, 1),
            error=str(e)[:300],
        )
        result = ""

    import glob as glob_mod
    from src.prompts.single_session_prompts import OUTPUT_FORMAT_MAP
    output_filename = OUTPUT_FORMAT_MAP.get(output_format, OUTPUT_FORMAT_MAP["html"])["ext"]
    report_path = Path(report_dir) / output_filename
    # 스케줄 모드: 날짜 파일명(results_YYYY-MM-DD.html)도 확인
    if not report_path.exists():
        dated_files = sorted(glob_mod.glob(str(Path(report_dir) / "results_*.html")), reverse=True)
        if dated_files:
            report_path = Path(dated_files[0])
    # 형식이 HTML이 아닌 경우 해당 확장자도 확인
    if not report_path.exists() and output_format != "html":
        html_path = Path(report_dir) / "results.html"
        if html_path.exists():
            report_path = html_path
    if not report_path.exists():
        _logger.warning(
            "single_session_no_report_file",
            session_id=session_id,
            timed_out=timed_out,
        )
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        fallback = Path(report_dir) / "results.html"
        fallback.write_text(
            _build_fallback_html(
                result, user_task, session_id,
                is_timeout=timed_out, timeout_s=timeout,
            ),
            encoding="utf-8",
        )

    # PDF 후처리: HTML → PDF 변환
    if output_format == "pdf":
        html_file = Path(report_dir) / "results.html"
        if html_file.exists():
            try:
                from src.utils.pdf_converter import html_to_pdf_sync
                pdf_path = html_to_pdf_sync(str(html_file))
                if pdf_path:
                    _logger.info("single_session_pdf_generated", path=pdf_path)
            except Exception as e:
                _logger.warning("single_session_pdf_failed", error=str(e)[:200])

    # 저장된 파일 목록
    saved_files = [f.name for f in Path(report_dir).iterdir() if f.is_file()] if Path(report_dir).exists() else []
    files_info = ", ".join(saved_files) if saved_files else "없음"

    return {
        "report_file_path": report_dir,
        "phase": "user_review_results",
        "messages": [
            AIMessage(content=(
                f"작업 완료 ({round(elapsed, 1)}초)\n"
                f"📁 저장 위치: {report_dir}/\n"
                f"📎 파일: {files_info}"
            ))
        ],
    }


def _build_fallback_html(
    result: str,
    user_task: str,
    session_id: str,
    is_timeout: bool = False,
    timeout_s: int = 0,
) -> str:
    """CLI 세션이 HTML 파일을 직접 생성하지 못한 경우의 fallback.

    타임아웃이 원인이면 상단에 경고 배너를 표시하여 사용자가 결과의 불완전성을
    알 수 있게 합니다. 내용은 모델이 생성한 raw text (학습용 인사이트 블록 등
    포함) — "크래시"가 아니라 "부분 결과"임을 명확히 안내합니다.
    """
    from datetime import datetime
    import html as html_mod

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    escaped_task = html_mod.escape(user_task)
    escaped_result = html_mod.escape(result[:50000] if result else "(결과 없음)")

    banner_html = ""
    title_prefix = ""
    if is_timeout:
        title_prefix = "[미완료] "
        banner_html = (
            '<div class="warning">'
            '<div class="warning-title">⚠️ 작업이 시간 제한으로 중단되었습니다</div>'
            f'<p>AI가 최종 보고서를 완성하기 전에 타임아웃({timeout_s}초)이 발생했습니다. '
            '아래는 AI가 작업 중 생성한 <strong>부분 결과</strong>입니다. '
            '완전한 보고서를 위해서는 다음을 시도해보세요:</p>'
            '<ul>'
            '<li>작업 범위를 더 구체적으로 좁히기</li>'
            '<li>관점(perspectives) 개수 줄이기</li>'
            '<li>분석 깊이(depth)를 light 또는 standard로 변경</li>'
            '</ul>'
            '</div>'
        )
    elif not result:
        banner_html = (
            '<div class="warning">'
            '<div class="warning-title">⚠️ AI 응답이 비어있습니다</div>'
            '<p>CLI 세션이 결과를 반환하지 않았습니다. 다시 시도해보세요.</p>'
            '</div>'
        )

    return (
        '<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>{title_prefix}{escaped_task}</title>'
        '<style>'
        "body{font-family:'Apple SD Gothic Neo','Noto Sans KR',sans-serif;"
        "max-width:900px;margin:0 auto;padding:40px 24px;color:#1a1a2e;background:#f4f6f9;line-height:1.7}"
        ".header{background:linear-gradient(135deg,#0f3460,#16213e);color:#fff;padding:40px;border-radius:12px;margin-bottom:24px}"
        ".header h1{font-size:24px;margin:0 0 8px}"
        ".header .meta{font-size:12px;opacity:0.6}"
        ".warning{background:#fff8e1;border:1px solid #f59e0b;border-left:4px solid #f59e0b;padding:20px 24px;border-radius:8px;margin-bottom:24px;color:#7c2d12}"
        ".warning-title{font-weight:700;font-size:16px;margin-bottom:8px}"
        ".warning ul{margin:8px 0 0 20px;padding:0}"
        ".warning li{margin:4px 0}"
        ".content{background:#fff;padding:32px;border-radius:12px;border:1px solid #e0e5ee;white-space:pre-wrap;font-size:14px}"
        '</style></head><body>'
        f'<div class="header"><h1>{title_prefix}{escaped_task}</h1>'
        f'<div class="meta">Generated {generated_at} | Session {session_id}</div></div>'
        f'{banner_html}'
        f'<div class="content">{escaped_result}</div>'
        '</body></html>'
    )
