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
    # firecrawl-mcp는 scrape 외에도 search/map/extract를 노출한다.
    # 모델이 어느 쪽을 고를지 모르므로 주요 4개를 화이트리스트에 모두 올린다.
    # (browser_*/agent 계열은 의존성 크고 사용 빈도 낮아 제외.)
    "mcp__firecrawl__firecrawl_scrape",
    "mcp__firecrawl__firecrawl_search",
    "mcp__firecrawl__firecrawl_map",
    "mcp__firecrawl__firecrawl_extract",
    # github-mcp는 R/O 도구군만 허용 — create_pr/write 계열은 제외해 실수 방지.
    "mcp__github__search_code",
    "mcp__github__search_repositories",
    "mcp__github__search_users",
    "mcp__github__get_file_contents",
]

# finalize retry 단계는 네트워크 호출 없이 텍스트 정리만 하면 되므로
# WebSearch/Agent/firecrawl 같이 idle timeout 의 실제 원인이 되는 도구를 제외한다.
_FINALIZE_TOOLS = ["Read", "Write", "Bash", "Glob"]

# CLI 가 stream idle 로 끊어졌는지 분류할 때 쓰는 키워드.
# Anthropic API 의 stream idle 메시지가 부분 결과 경고로 함께 옴.
_STREAM_IDLE_MARKERS = (
    "stream idle",
    "stream_idle",
    "partial response received",
    "partial response",
)

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
    effort: str | None = None,
) -> tuple[str, bool, bool]:
    """CLI subprocess를 스트리밍으로 실행하고 활동 이벤트를 emit.

    Returns:
        (full_text, timed_out, stream_idle):
        - timed_out: 우리 측 wall-clock timeout 으로 중단된 경우
        - stream_idle: Anthropic API 가 stream idle / partial response 로
          모델 응답을 중간에 잘라낸 경우. 이 경우 finalize retry 가 의미 있음.
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
    if effort:
        cmd.extend(["--effort", effort])
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
    stream_idle = False
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
                        lower = (result_text or "").lower()
                        if any(marker in lower for marker in _STREAM_IDLE_MARKERS):
                            stream_idle = True
                            _logger.warning(
                                "single_session_stream_idle",
                                error=result_text[:200],
                                accumulated_chars=sum(len(t) for t in text_accumulator),
                            )
                        else:
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

    return full_text, timed_out, stream_idle


async def _finalize_retry(
    *,
    user_task: str,
    report_dir: str,
    partial_text: str,
    session_id: str,
    model: str,
    effort: str | None = None,
) -> bool:
    """Stream idle 로 끊긴 세션을 짧은 retry 로 마무리.

    네트워크 호출 없이 partial_text 를 그대로 받아 ``results.html`` 한 장만
    다시 쓰게 한다. 도구는 Read/Write/Bash/Glob 만 허용하여 idle timeout 의
    재발 위험을 차단한다. 디자인/구조는 LLM 이 자유롭게 결정한다.

    Returns:
        True 면 ``{report_dir}/results.html`` 이 새로 생성됨. False 면 실패.
    """
    prompt = (
        f"이전 리서치 세션이 응답 스트림 중간에 끊겼습니다. "
        f"새로운 리서치를 절대 수행하지 마세요. 웹 검색이나 fetch 도구도 사용 금지. "
        f"이미 수집된 아래 부분 결과만을 사용해서 "
        f"`{report_dir}/results.html` 한 파일만 만드세요.\n\n"
        f"먼저 `mkdir -p {report_dir}` 실행. "
        f"그 다음 Write 도구로 한 번만 저장하면 끝입니다.\n\n"
        f"## 원래 작업\n{user_task}\n\n"
        f"## 부분 결과 (이것만 활용)\n{partial_text[:30000]}\n\n"
        f"리포트의 구조·디자인·문체·레이아웃은 자유롭게 결정하세요. "
        f"단일 파일 self-contained HTML 이어야 하고, PDF 로 변환해도 읽기 좋게 만들어주세요. "
        f"불완전한 보고서임을 서두에 한 줄로만 알려주세요. 새 정보를 지어내지 마세요."
    )
    finalize_system = (
        "당신은 부분 결과를 받아 self-contained HTML 리포트 한 장으로 마무리하는 정리 담당입니다. "
        "새로운 리서치는 절대 수행하지 마세요. Write 도구 한 번만 사용합니다."
    )

    try:
        text, timed_out, _ = await _stream_session(
            prompt=prompt,
            system_prompt=finalize_system,
            session_id=session_id,
            model=model,
            max_turns=3,
            timeout=180,
            effort=effort,
        )
    except Exception as exc:
        _logger.warning("single_session_finalize_retry_failed", error=str(exc)[:200])
        return False

    target = Path(report_dir) / "results.html"
    if target.exists() and target.stat().st_size > 0:
        _logger.info("single_session_finalize_retry_success", path=str(target), text_len=len(text))
        return True
    _logger.warning(
        "single_session_finalize_retry_no_file",
        report_dir=report_dir,
        timed_out=timed_out,
        text_len=len(text),
    )
    return False


def _write_minimal_fallback_html(
    *,
    html_target: Path,
    user_task: str,
    raw_text: str,
    reason: str,
) -> None:
    """LLM 이 results.html 을 남기지 못한 경우의 최소한의 안전망.

    템플릿/렌더러/디자인 규칙 없이, 받은 raw_text 를 그대로 ``<pre>`` 안에
    담아 사용자가 최소한 읽을 수는 있게 한다. 의도적으로 시각 자산을
    쓰지 않는다 — 진짜 예쁜 리포트는 LLM 이 작성할 때만 존재한다는 계약.
    """
    import html as _html

    reason_note = {
        "timeout": "작업이 시간 제한으로 중단되었습니다.",
        "stream_idle_timeout": "AI 응답 스트림이 중간에 끊겼습니다.",
        "empty_result": "AI 응답이 비어 있습니다.",
        "no_artifact": "AI 가 결과 파일을 저장하지 못했습니다.",
    }.get(reason, "결과 파일을 만들지 못했습니다.")

    safe_task = _html.escape(user_task or "")
    safe_text = _html.escape(raw_text or "")
    body = (
        "<!doctype html><html lang='ko'><meta charset='utf-8'>"
        f"<title>{safe_task or 'Report'}</title>"
        "<body style='font-family:system-ui,sans-serif;max-width:860px;margin:40px auto;padding:0 20px;line-height:1.6'>"
        f"<h1 style='font-size:1.4rem'>{safe_task or '리포트 생성 실패'}</h1>"
        f"<p><strong>{reason_note}</strong> 아래는 모델이 작업 중 마지막으로 출력한 원문입니다.</p>"
        f"<pre style='white-space:pre-wrap;background:#f6f6f6;padding:16px;border-radius:6px'>{safe_text}</pre>"
        "</body></html>"
    )
    html_target.write_text(body, encoding="utf-8")


def _resolve_report_html(
    *,
    report_dir: str,
    user_task: str,
    session_id: str,
    raw_text: str,
    timed_out: bool,
    stream_idle: bool,
    timeout_s: int,
) -> Path:
    """CLI 세션이 끝난 뒤 사용자에게 보여줄 results.html 을 보장한다.

    싱글 세션은 LLM 이 ``results.html`` 을 직접 작성한다. Python 쪽은
    렌더링·템플릿·스타일에 관여하지 않는다. 두 갈래만 존재:
      1. LLM 이 작성한 ``results.html`` 이 있으면 → 그대로 사용
      2. 없으면 → raw_text 를 <pre> 로 담는 최소 fallback HTML 만 기록
    """
    rd = Path(report_dir)
    rd.mkdir(parents=True, exist_ok=True)
    html_target = rd / "results.html"

    if html_target.exists() and html_target.stat().st_size > 0:
        _logger.info("single_session_llm_html_kept", path=str(html_target))
        return html_target

    if timed_out:
        reason = "timeout"
    elif stream_idle:
        reason = "stream_idle_timeout"
    elif not raw_text:
        reason = "empty_result"
    else:
        reason = "no_artifact"

    _write_minimal_fallback_html(
        html_target=html_target,
        user_task=user_task,
        raw_text=raw_text,
        reason=reason,
    )
    _logger.warning(
        "single_session_fallback_rendered",
        reason=reason,
        path=str(html_target),
    )
    return html_target


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

    # 폴더명: "{제목}_{생산일}" (src/utils/report_paths.py)
    from src.utils.report_paths import build_report_dir
    report_dir = str(build_report_dir(user_task, session_id=session_id))
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

    # Delta 비교: 이전 실행 보고서가 있으면 같은 디렉터리로 모으고 비교 지시를 주입
    previous_report_path = pre_context.get("previous_report_path")
    is_scheduled = state.get("execution_mode") == "scheduled"

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
    stream_idle = False
    result = ""

    try:
        result, timed_out, stream_idle = await _stream_session(
            prompt=prompt,
            system_prompt=SINGLE_SESSION_SYSTEM,
            session_id=session_id,
            model=settings.worker_model,
            max_turns=max_turns,
            timeout=timeout,
            effort=settings.worker_effort,
        )
        elapsed = time.time() - start_time
        _logger.info(
            "single_session_complete",
            session_id=session_id,
            elapsed_s=round(elapsed, 1),
            result_len=len(result),
            timed_out=timed_out,
            stream_idle=stream_idle,
        )
    except Exception as e:
        elapsed = time.time() - start_time
        _logger.error(
            "single_session_failed",
            session_id=session_id,
            elapsed_s=round(elapsed, 1),
            error=str(e)[:300],
        )

    # ── 결과 파일 리졸브 ──
    # 싱글 세션은 results.html 을 LLM 이 직접 작성한다. 파일이 없고 부분
    # 결과만 있으면 짧은 finalize retry 로 HTML 한 장만 다시 쓰게 한다.
    html_path = Path(report_dir) / "results.html"
    if not html_path.exists() and stream_idle and result:
        emit_mode_event(session_id, {
            "type": "activity",
            "data": {
                "action": "finalizing",
                "message": "🩹 응답이 중단되어 부분 결과를 정리하는 중…",
                "elapsed": round(time.time() - start_time, 1),
            },
        })
        await _finalize_retry(
            user_task=user_task,
            report_dir=report_dir,
            partial_text=result,
            session_id=session_id,
            model=settings.worker_model,
            effort=settings.worker_effort,
        )

    _resolve_report_html(
        report_dir=report_dir,
        user_task=user_task,
        session_id=session_id,
        raw_text=result,
        timed_out=timed_out,
        stream_idle=stream_idle,
        timeout_s=timeout,
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


