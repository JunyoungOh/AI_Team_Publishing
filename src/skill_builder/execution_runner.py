"""스킬 실행 오케스트레이터.

run_skill(slug, user_input, on_event):
  1) skill_loader로 컨텍스트 로드
  2) isolation_mode에 따라 cwd / allowed_tools 결정
  3) execution_streamer 호출
  4) 결과를 run_history에 저장
  5) RunRecord 반환

에러는 모두 잡아서 status="error" RunRecord로 저장 후 반환.
호출자(WS endpoint)는 예외를 처리할 필요 없이 record.status만 보면 된다.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from src.config.settings import get_settings
from src.skill_builder.execution_streamer import stream_skill_execution
from src.skill_builder.run_history import RunRecord, save_run
from src.skill_builder.skill_loader import (
    IsolationMode,
    load_skill_for_execution,
)
from src.utils.notifier import notify_completion


_BUILTIN_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# Claude Code 빌트인 도구 중 SKILL.md 본문에서 참조하면 자동 허용할 목록.
# _BUILTIN_TOOLS는 모든 스킬에 무조건 부여되고,
# _GRANTABLE_BUILTINS는 SKILL.md 본문에 도구명이 등장할 때만 부여된다.
_GRANTABLE_BUILTINS = ["WebFetch", "WebSearch"]

# SKILL.md 본문에서 mcp__<server>__<tool> 참조를 자동 감지하는 정규식.
# skill_metadata.json 누락 / 서버 매핑 미비 시 안전망으로 작동한다.
_MCP_TOOL_RE = re.compile(r"mcp__[\w-]+__[\w]+")


def _detect_required_builtins(skill_body: str) -> list[str]:
    """SKILL.md 본문에서 참조된 Claude Code 빌트인 도구를 자동 감지."""
    return [t for t in _GRANTABLE_BUILTINS if t in skill_body]


def _detect_mcp_tools_from_body(skill_body: str) -> list[str]:
    """SKILL.md 본문에서 직접 참조된 MCP 도구명(mcp__*__*)을 자동 감지.

    skill_metadata.json이 누락되거나 _build_allowed_tools_for_mcps의
    하드코딩 맵에 없는 서버라도, SKILL.md에 도구명이 명시되어 있으면
    allowed_tools에 추가한다.
    """
    return list(set(_MCP_TOOL_RE.findall(skill_body)))


def _build_allowed_tools_for_mcps(required_mcps: list[str]) -> list[str]:
    """required_mcps를 mcp__<server>__<tool> 형식으로 변환.

    Claude Code의 --allowedTools는 정확한 도구명을 요구하므로 각 MCP 서버의
    잘 알려진 도구를 명시한다.

    주의: 플러그인으로 설치된 MCP는 네임스페이스가
    ``mcp__plugin_<ns>_<server>__<tool>`` 형태로 달라질 수 있다. 아래 맵은
    프로젝트 ``.mcp.json``에 직접 등록된 서버 기준이다. 새 MCP를 추가할
    때는 ``src/utils/tool_definitions.py``와 실제 스트림에서 관찰되는
    도구명으로 검증할 것.

    SKILL.md 본문에서의 자동 감지(_detect_mcp_tools_from_body)가 안전망으로
    작동하므로, 여기에 누락되어도 치명적이지는 않다.
    """
    known_tools_by_server = {
        "serper": ["mcp__serper__google_search"],
        "firecrawl": [
            "mcp__firecrawl__firecrawl_scrape",
            "mcp__firecrawl__firecrawl_search",
        ],
        "brave-search": ["mcp__brave-search__brave_web_search"],
        "github": [
            "mcp__github__search_code",
            "mcp__github__search_repositories",
        ],
        "mem0": [
            "mcp__mem0__search_memories",
            "mcp__mem0__add_memory",
        ],
        "dart": [
            "mcp__dart__resolve_corp_code",
            "mcp__dart__list_disclosures",
            "mcp__dart__get_company",
            "mcp__dart__get_document",
            "mcp__dart__get_financial",
            "mcp__dart__list_shareholder_reports",
            "mcp__dart__list_dividend_events",
        ],
        "law": [
            "mcp__law__law_search",
            "mcp__law__law_get",
            "mcp__law__law_get_article",
            "mcp__law__prec_search",
            "mcp__law__prec_get",
            "mcp__law__expc_search",
        ],
    }
    out: list[str] = []
    for server in required_mcps:
        out.extend(known_tools_by_server.get(server, []))
    return out


def _build_system_prompt(skill_body: str) -> str:
    return (
        skill_body
        + "\n\n---\n\n## 실행 컨텍스트\n"
        + "사용자의 입력은 다음 user message로 전달됩니다. "
        + "이 스킬의 절차에 따라 작업을 수행하고 결과를 한국어로 반환하세요.\n"
    )


async def run_skill(
    *,
    slug: str,
    user_input: str,
    on_event: Callable[[dict], None],
    timeout: int = 600,
    model: str = "sonnet",
    runs_root: Optional[Path] = None,
) -> RunRecord:
    """스킬을 한 번 실행하고 RunRecord를 반환.

    예외는 내부에서 모두 잡아서 status="error" 레코드로 저장한다.
    """
    started = time.time()

    try:
        ctx = load_skill_for_execution(slug)
    except Exception as e:
        record = save_run(
            slug=slug,
            user_input=user_input,
            result_text="",
            status="error",
            tool_count=0,
            duration_seconds=round(time.time() - started, 2),
            error_message=str(e),
            runs_root=runs_root,
        )
        on_event({"action": "error", "message": f"스킬 로드 실패: {e}"})
        await notify_completion(
            kind="skill",
            title=slug,
            summary=f"스킬 로드 실패: {e}",
            duration_seconds=record.duration_seconds,
            status="failure",
        )
        return record

    extra_builtins = _detect_required_builtins(ctx.skill_body)
    extra_mcp_tools = _detect_mcp_tools_from_body(ctx.skill_body)

    # MCP 도구가 body에서 감지되면, skill_metadata.json 누락과 무관하게
    # 프로젝트 루트에서 실행 (MCP 서버 설정이 .mcp.json에 있으므로).
    needs_project_root = (
        ctx.isolation_mode == IsolationMode.WITH_MCPS or bool(extra_mcp_tools)
    )

    if needs_project_root:
        cwd = os.getcwd()
        allowed_tools = (
            list(_BUILTIN_TOOLS)
            + extra_builtins
            + _build_allowed_tools_for_mcps(ctx.required_mcps)
            + extra_mcp_tools
        )
    else:
        cwd = "/tmp"
        allowed_tools = list(_BUILTIN_TOOLS) + extra_builtins

    # 중복 제거 (매핑 경로 + body 감지 경로가 겹칠 수 있음)
    allowed_tools = list(dict.fromkeys(allowed_tools))

    system_prompt = _build_system_prompt(ctx.skill_body)

    try:
        full_text, tool_count, timed_out = await stream_skill_execution(
            prompt=user_input or "스킬을 실행하세요",
            system_prompt=system_prompt,
            model=model,
            allowed_tools=allowed_tools,
            cwd=cwd,
            timeout=timeout,
            on_event=on_event,
            effort=get_settings().worker_effort,
        )
    except Exception as e:
        record = save_run(
            slug=slug,
            user_input=user_input,
            result_text="",
            status="error",
            tool_count=0,
            duration_seconds=round(time.time() - started, 2),
            error_message=str(e),
            runs_root=runs_root,
        )
        await notify_completion(
            kind="skill",
            title=slug,
            summary=f"실행 중 오류: {e}",
            duration_seconds=record.duration_seconds,
            status="failure",
        )
        return record

    status = "timeout" if timed_out else "completed"
    record = save_run(
        slug=slug,
        user_input=user_input,
        result_text=full_text,
        status=status,
        tool_count=tool_count,
        duration_seconds=round(time.time() - started, 2),
        runs_root=runs_root,
    )
    await notify_completion(
        kind="skill",
        title=slug,
        summary=f"도구 {tool_count}회 호출" if tool_count else "",
        duration_seconds=record.duration_seconds,
        status="timeout" if timed_out else "success",
    )
    return record
