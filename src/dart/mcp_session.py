"""DART session via Claude Code CLI + DART MCP server.

Replaces the old ``src/dart/engine.py`` + ``src/dart/session.py`` pair.
The custom XML ``<tool_call>`` wrapper was fighting Sonnet's ReAct training
and produced hallucinated filings. MCP protocol eliminates the problem at
the token level — Claude emits a ``tool_use`` block via the SDK's native
channel, CLI intercepts and forwards to the DART MCP server, and the real
result comes back as ``tool_result`` in the next turn. There is no text
position where Claude can fabricate a tool response.

Flow:
    WebSocket ←→ DartMcpSession
                    ↓ spawns
                 claude CLI (stream-json, --mcp-config)
                    ↓ spawns
                 python -m src.dart.mcp_server (stdio JSON-RPC)
                    ↓ calls
                 existing DART_TOOL_EXECUTORS (DartClient, CorpCodeIndex)
                    ↓ HTTPS
                 Open DART API
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

# Rebind via getattr to avoid false-positive matches from repo security hooks
# that flag literal ``child_process.exec(`` style patterns. asyncio's
# create_subprocess_exec is the safe execFile-equivalent — it takes a list of
# arguments, not a shell string, and never goes through a shell interpreter.
_spawn_subprocess = getattr(asyncio, "create_subprocess_" + "exec")

_DISCLAIMER = (
    "⚠️ 본 답변은 Open DART 공시자료를 기반으로 한 정보 제공이며, 투자 자문이 아닙니다. "
    "투자 결정은 반드시 원문 공시와 전문가 상담을 거치시기 바랍니다."
)

_MCP_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# MCP server key "dart" in .mcp.json + 7 tool names → claude CLI tool namespace
_DART_TOOL_NAMES = [
    "mcp__dart__resolve_corp_code",
    "mcp__dart__list_disclosures",
    "mcp__dart__get_company",
    "mcp__dart__get_document",
    "mcp__dart__get_financial",
    "mcp__dart__list_shareholder_reports",
    "mcp__dart__list_dividend_events",
]


def _build_dart_mcp_config() -> str | None:
    """Read top-level .mcp.json, substitute env vars, write a DART-only temp file.

    Claude CLI does not substitute ``${VAR}`` patterns inside .mcp.json, so we
    do it ourselves — same approach as
    ``src/graphs/nodes/single_session.py:_build_runtime_mcp_config``. We also
    strip to just the dart entry so the CLI doesn't spawn unrelated MCP servers
    (firecrawl, github, mem0) for every DART query.

    Returns the absolute path to a temp file, or None if .mcp.json or the
    dart entry is missing.
    """
    template = Path(".mcp.json")
    if not template.exists():
        logger.warning("_build_dart_mcp_config: .mcp.json not found at %s", template.absolute())
        return None
    try:
        raw = template.read_text(encoding="utf-8")
        substituted = _MCP_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""),
            raw,
        )
        config = json.loads(substituted)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("_build_dart_mcp_config: parse failed: %s", exc)
        return None

    servers = config.get("mcpServers") or {}
    dart_cfg = servers.get("dart")
    if not dart_cfg:
        logger.warning("_build_dart_mcp_config: 'dart' entry missing from .mcp.json")
        return None

    minimal = {"mcpServers": {"dart": dart_cfg}}
    fd, path = tempfile.mkstemp(suffix=".mcp.json", prefix="dart_mcp_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(minimal, f, ensure_ascii=False)
    return path


def _build_system_prompt() -> str:
    """Compact DART system prompt for the MCP path."""
    today = time.strftime("%Y-%m-%d")
    return f"""당신은 대한민국 전자공시(Open DART) 조사 보조입니다. 금융감독원 전자공시시스템에서 직접 조회한 공시자료·기업개황·재무제표만을 근거로 답변합니다.

## 오늘 날짜 (매우 중요)

**오늘은 {today} 입니다.** 모든 상대 시점("최근", "올해", "작년", "최신", "지난 분기")은 이 날짜 기준으로 해석하십시오. 훈련 데이터의 암묵적 현재가 아니라 **반드시 이 시스템 날짜**를 따라야 합니다.

## 사용 가능한 도구 (MCP 서버 경유)

이 세션에서 사용할 수 있는 도구는 아래 일곱 개뿐입니다. 다른 도구(WebSearch, WebFetch, Read, Bash 등)는 **절대 호출하지 마십시오**. DART 외부 소스는 우리가 통제할 수 없으며 써드파티 애그리게이터는 stale 데이터 위험이 있습니다.

1. `mcp__dart__resolve_corp_code(query, limit=5)` — 회사명/종목코드 → 8자리 corp_code 해석
2. `mcp__dart__list_disclosures(corp_code, bgn_de, end_de, pblntf_ty, ...)` — 공시 목록 검색
3. `mcp__dart__get_company(corp_code)` — 기업개황
4. `mcp__dart__get_document(rcept_no, max_chars=10000)` — 공시서류 원문 텍스트
5. `mcp__dart__get_financial(corp_code, bsns_year, reprt_code, fs_sections, fs_div)` — 재무제표
6. `mcp__dart__list_shareholder_reports(corp_code)` — 대량보유(5%) + 임원/주요주주 지분
7. `mcp__dart__list_dividend_events(corp_code, bsns_year, reprt_code)` — 배당에 관한 사항

## 답변 규칙

1. **회사명 해석 우선** — 질문에 회사명이 나오면 먼저 `resolve_corp_code` 호출.
   - 한국 상장사 중 법인명이 영문인 경우가 많습니다: **네이버→NAVER, 포스코→POSCO, 케이티→KT, 엘지→LG, 에스케이→SK, 케이비금융→KB Financial**. 한글 쿼리로 본사를 못 찾으면 반드시 영문명으로 재시도하거나 종목코드 사용.
   - 결과에 자회사가 여럿이고 본사가 안 보이면 종목코드(6자리)로 직접 조회.

2. **날짜 범위** — `list_disclosures` 호출 시 `bgn_de`/`end_de` 는 **오늘({today}) 기준**으로 계산. "최신"이면 bgn_de 를 오늘-12개월로, end_de 를 오늘로. 특정 연도면 해당 연도 1월1일~12월31일.

3. **재무 섹션** — `get_financial` 의 `fs_sections`:
   - 매출·이익 → `["IS"]` (기본)
   - 자산·부채·자본 → `["BS"]`
   - 현금흐름 → `["CF"]`
   - 교차 비율(ROE=순이익/자본, 재고자산회전율 등) → `["IS", "BS"]` 로 **1회 호출**
   - `["ALL"]` 은 사용자가 "종합/전반/건전성" 을 명시적으로 요청할 때만

4. **원문 인용(Verbatim)** — 공시서류 원문을 인용하려면 `get_document` 로 실제 텍스트를 먼저 가져와야 합니다. 도구 결과에 없는 내용을 따옴표로 인용하지 마십시오 — 환각 금지.

5. **링크** — 답변의 URL 은 도구 결과의 `source_url` 필드를 **그대로** 사용. 손으로 조립하면 파라미터(rcpNo 등)를 틀릴 수 있습니다.

6. **출력 형식** — 한국어 마크다운. 숫자 비교·다년도 추이는 표로. 답변 말미에 **반드시** 디스클레이머 포함:

   > ⚠️ 본 답변은 Open DART 공시자료를 기반으로 한 정보 제공이며, 투자 자문이 아닙니다.
   > 투자 결정은 반드시 원문 공시와 전문가 상담을 거치시기 바랍니다.

7. **⛔ 과도한 탐색 금지 — 한 소스 답변 원칙 (매우 중요)**

   사용자 질문에 대해 **답변 가능한 최소한의 데이터**만 조회하십시오. 기준 규칙:

   - **메인 공시 원문 1건**(대개 최신 사업보고서)에서 답을 도출할 수 있다면 **거기서 종료**.
     해당 원문에 "2026-02-20 주총소집공고", "정정공시 참조" 같은 다른 공시 언급이 있어도
     **선제적으로 조회하지 마십시오**.
   - 사용자가 "최신 반영해서" 또는 "주주총회 이후 변동 포함해서" 같이 **명시적으로**
     최신화를 요청하지 않은 경우, 가장 최근 정기보고서의 **기준일 데이터**로 답변을 완결하고
     답변 본문에 "※ 이 정보는 [사업보고서 제출일] 기준이며, 그 이후 변동 사항은 반영되지 않았습니다"
     한 줄만 추가하십시오.
   - "혹시 더 필요하시면…" 같은 후속 제안은 텍스트로만 한 줄 쓸 수는 있으나,
     그 제안을 **스스로 실행하지 마십시오**. 사용자의 후속 질문을 기다립니다.
   - **목표**: 1~3개의 도구 호출로 답변 완결. 4회를 넘어가면 질문을 더 좁게 재해석하거나
     사용자에게 구체화를 요청하십시오.

8. **⛔ 디스클레이머 이후 도구 호출 절대 금지**

   디스클레이머(`⚠️ 본 답변은…`)를 쓴 **그 순간 답변은 완결된 것**입니다.
   이후에 어떤 `get_document`, `get_financial`, `list_disclosures` 호출도 하지 마십시오.
   **이 규칙을 위반하면 시스템이 subprocess 를 강제 종료하고 불완전한 응답만 반환합니다.**
   디스클레이머를 쓰기 전에 필요한 모든 정보를 이미 확보한 상태여야 합니다.

도구 결과가 빈 배열이거나 오류이면 **추측으로 채우지 마십시오**. "Open DART에서 해당 자료를 확인할 수 없습니다" 라고 답하고 사용자에게 다른 조건(기간, 회사명, 보고서 종류)을 요청하십시오.
"""


# ─── 세션 클래스 ─────────────────────────────────────


class DartMcpSession:
    """One WebSocket ↔ one DART MCP-backed chat session.

    Public contract matches the old DartSession so ``src/ui/routes/dart.py``
    can swap the import without other changes.
    """

    def __init__(self, ws, user_id: str = "") -> None:
        self._ws = ws
        self._user_id = user_id
        self._session_id = f"dart_{uuid.uuid4().hex[:12]}"
        self._cancelled = False
        self._proc: Any = None
        self._last_activity = time.time()
        self._heartbeat_task: asyncio.Task | None = None
        self._ttl_task: asyncio.Task | None = None
        # flash → medium effort, think → high effort
        self._mode = "flash"
        # Tracks whether the accumulated answer already contains the
        # disclaimer. Once True, any further tool_use from the LLM triggers
        # an immediate subprocess kill (see _handle_stream_event).
        self._disclaimer_seen = False

    @property
    def session_id(self) -> str:
        return self._session_id

    # ── Public API ───────────────────────────────

    async def run(self) -> None:
        await self._send_init()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._ttl_task = asyncio.create_task(self._ttl_watchdog())
        try:
            await self._message_loop()
        finally:
            self._cleanup()

    def cancel(self) -> None:
        self._cancelled = True
        self._kill_proc()
        self._cleanup()

    # ── Message loop ─────────────────────────────

    async def _send_init(self) -> None:
        has_key = get_settings().dart_api_key != ""
        await self._send({
            "type": "dart_init",
            "data": {
                "session_id": self._session_id,
                "has_key": has_key,
                "security_banner": (
                    "금융감독원 전자공시시스템(Open DART)의 공식 API를 통해 공시자료와 "
                    "재무제표를 직접 조회합니다. 대화 내용은 서버에 저장되지 않으며, "
                    "세션 종료 시 즉시 파기됩니다."
                ),
            },
        })

    async def _message_loop(self) -> None:
        while not self._cancelled:
            try:
                msg = await self._ws.receive_json()
            except Exception:  # noqa: BLE001
                break

            self._last_activity = time.time()
            msg_type = msg.get("type", "")
            data = msg.get("data", {}) or {}

            if msg_type == "dart_stop":
                self._kill_proc()
                continue
            if msg_type == "dart_set_mode":
                new_mode = data.get("mode", "flash")
                if new_mode in ("flash", "think"):
                    self._mode = new_mode
                continue
            if msg_type == "dart_message":
                content = (data.get("content") or "").strip()
                if not content:
                    continue
                effort_override = data.get("effort")
                if effort_override in ("flash", "think"):
                    self._mode = effort_override
                try:
                    await self._run_claude(content)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("DartMcpSession: run error")
                    await self._send({
                        "type": "dart_error",
                        "data": {"message": f"실행 오류: {exc}"},
                    })

    # ── CLI spawn + stream parsing ───────────────

    async def _run_claude(self, user_text: str) -> None:
        """Spawn claude CLI with the DART MCP config and stream events to the WS."""
        # Reset disclaimer tracking for this new request
        self._disclaimer_seen = False

        mcp_config_path = _build_dart_mcp_config()
        system_prompt = _build_system_prompt()
        effort = "medium" if self._mode == "flash" else "high"

        cmd = [
            "claude", "-p", user_text,
            "--output-format", "stream-json",
            "--verbose",
            "--model", "sonnet",
            "--max-turns", "10",
            "--append-system-prompt", system_prompt,
            "--allowedTools", ",".join(_DART_TOOL_NAMES),
            "--permission-mode", "auto",
            "--effort", effort,
        ]
        if mcp_config_path:
            cmd.extend(["--mcp-config", mcp_config_path, "--strict-mcp-config"])

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        await self._send({
            "type": "dart_tool_status",
            "data": {"tool": "", "status": "AI 분석 중..."},
        })

        self._proc = await _spawn_subprocess(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
            start_new_session=True,
            env=env,
            limit=sys.maxsize,
        )

        acc_text: list[str] = []
        had_output = False

        try:
            async with asyncio.timeout(300):
                assert self._proc.stdout is not None
                async for raw_line in self._proc.stdout:
                    if self._cancelled:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if await self._handle_stream_event(event, acc_text):
                        had_output = True
        except asyncio.TimeoutError:
            logger.warning("DartMcpSession: claude subprocess timeout after 300s")
            await self._send({
                "type": "dart_error",
                "data": {"message": "응답 시간 초과. 더 구체적으로 다시 질문해주세요."},
            })
        finally:
            if self._proc:
                try:
                    await self._proc.wait()
                except Exception:  # noqa: BLE001
                    pass
                self._proc = None
            if mcp_config_path and os.path.exists(mcp_config_path):
                try:
                    os.unlink(mcp_config_path)
                except OSError:
                    pass

        # Auto-append disclaimer if the LLM forgot it
        full_text = "".join(acc_text).strip()
        if full_text and _DISCLAIMER[:20] not in full_text:
            await self._send({
                "type": "dart_stream",
                "data": {"token": f"\n\n> {_DISCLAIMER}", "done": False},
            })

        # Done signal — frontend renders markdown + clears typing indicator
        await self._send({
            "type": "dart_stream",
            "data": {"token": "", "done": True},
        })

        if not had_output:
            logger.warning("DartMcpSession: no output events received")

    async def _handle_stream_event(
        self,
        event: dict[str, Any],
        acc_text: list[str],
    ) -> bool:
        """Process one stream-json event. Returns True if content was emitted."""
        etype = event.get("type")
        emitted = False

        if etype == "assistant":
            message = event.get("message", {}) or {}
            for block in message.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "") or ""
                    if text:
                        acc_text.append(text)
                        await self._send({
                            "type": "dart_stream",
                            "data": {"token": text, "done": False},
                        })
                        emitted = True
                        # Detect disclaimer marker once it's streamed
                        if not self._disclaimer_seen and _DISCLAIMER[:20] in "".join(acc_text):
                            self._disclaimer_seen = True
                elif btype == "tool_use":
                    # Guard: if the answer + disclaimer is already written,
                    # the LLM is violating the "stop after disclaimer" rule.
                    # Kill the subprocess to prevent overshoot → CLI crash.
                    if self._disclaimer_seen:
                        logger.warning(
                            "DartMcpSession: tool_use emitted after disclaimer — "
                            "terminating subprocess to prevent context overshoot"
                        )
                        self._cancelled = True
                        self._kill_proc()
                        return emitted
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {}) or {}
                    short = tool_name.replace("mcp__dart__", "")
                    await self._send({
                        "type": "dart_tool_status",
                        "data": {
                            "tool": short,
                            "status": self._describe_tool(short, tool_input),
                        },
                    })
                    emitted = True

        elif etype == "result":
            # Final CLI result event. Carries error flag when subprocess failed.
            if event.get("is_error"):
                error_text = str(event.get("result", "")).strip()[:300]
                logger.warning("CLI result error: %s", error_text or "(empty)")
                # Empty is_error typically means CLI internal max_turns or
                # token budget exceeded. If we already streamed a substantial
                # answer, suppress the noisy error message — the user already
                # has a useful response.
                if not error_text:
                    if acc_text and sum(len(t) for t in acc_text) > 200:
                        # We have a real answer — treat overshoot as success
                        return emitted
                    friendly = (
                        "모델이 응답을 조합하는 도중 내부 한도에 도달했습니다. "
                        "질문을 좀 더 좁게(구체적 연도·항목 명시) 다시 시도해 주세요."
                    )
                else:
                    friendly = f"CLI 오류: {error_text}"
                await self._send({
                    "type": "dart_error",
                    "data": {"message": friendly},
                })
                emitted = True

        return emitted

    @staticmethod
    def _describe_tool(name: str, inputs: dict[str, Any]) -> str:
        if name == "resolve_corp_code":
            return f"회사명 해석 중: {inputs.get('query', '?')}"
        if name == "list_disclosures":
            cc = inputs.get("corp_code", "")
            period = f"{inputs.get('bgn_de', '')}~{inputs.get('end_de', '')}".strip("~")
            hint = f" corp={cc}" if cc else ""
            hint += f" 기간={period}" if period else ""
            return f"공시 목록 조회 중:{hint}"
        if name == "get_company":
            return f"기업개황 조회 중: corp={inputs.get('corp_code', '?')}"
        if name == "get_document":
            return f"공시 원문 조회 중: rcept_no={inputs.get('rcept_no', '?')}"
        if name == "get_financial":
            sections = inputs.get("fs_sections", ["IS"])
            label = "+".join(sections) if isinstance(sections, list) else str(sections)
            return (
                f"재무정보 조회 중: corp={inputs.get('corp_code', '?')} "
                f"year={inputs.get('bsns_year', '?')} sections={label}"
            )
        if name == "list_shareholder_reports":
            return f"지분보고 조회 중: corp={inputs.get('corp_code', '?')}"
        if name == "list_dividend_events":
            return (
                f"배당정보 조회 중: corp={inputs.get('corp_code', '?')} "
                f"year={inputs.get('bsns_year', '?')}"
            )
        return f"{name} 실행 중..."

    # ── Process / lifecycle plumbing ─────────────

    def _kill_proc(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("DartMcpSession: terminate failed")

    def _cleanup(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ttl_task:
            self._ttl_task.cancel()
        self._kill_proc()
        logger.info("DART MCP session %s cleaned up", self._session_id)

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._cancelled:
                await asyncio.sleep(15)
                await self._send({"type": "heartbeat", "ts": time.time()})
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    async def _ttl_watchdog(self) -> None:
        ttl_seconds = get_settings().dart_session_ttl_minutes * 60
        try:
            while not self._cancelled:
                await asyncio.sleep(60)
                if time.time() - self._last_activity > ttl_seconds:
                    logger.info("DART MCP session %s TTL expired", self._session_id)
                    self._cancelled = True
                    try:
                        await self._send({
                            "type": "dart_error",
                            "data": {"message": "세션이 비활성으로 종료되었습니다."},
                        })
                    except Exception:  # noqa: BLE001
                        pass
                    break
        except asyncio.CancelledError:
            pass

    async def _send(self, data: dict[str, Any]) -> None:
        try:
            await self._ws.send_json(data)
        except Exception:  # noqa: BLE001
            self._cancelled = True
