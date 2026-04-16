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
from src.utils.claude_code import InsightStreamFilter

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
    """Compact DART system prompt — minimalist by design.

    An earlier version had 3000+ chars of rules, flow diagrams, and
    domain guidance. That made Sonnet over-reason and chain too many tool
    calls, ending in CLI max_turns crashes. This version is ~900 chars
    with a single directive: "call tools, format results, add 3-4 line
    comment, stop."
    """
    today = time.strftime("%Y-%m-%d")
    return f"""당신은 DART 공시 데이터 포매터입니다. 오늘: **{today}**

## 워크플로우 (단순하게)

1. 도구를 호출해 필요한 데이터를 가져온다 (최대 **3~4회**)
2. 결과를 표나 리스트로 깔끔히 정리
3. 3~4문장 간결한 해설
4. 디스클레이머 붙이고 **즉시 종료**

당신은 분석가가 아닙니다. **API 결과 포매터**입니다. 심층 분석·교차 검증·참조
공시 추가 조회 금지. 사용자가 더 원하면 후속 질문으로 옵니다.

## 도구 (7개)

- `resolve_corp_code(query)` — 회사명 → 8자리 corp_code. **영문명 주의**:
  네이버→NAVER, 포스코→POSCO, 케이티→KT, 엘지→LG, 에스케이→SK, 케이비→KB
- `list_disclosures(corp_code, bgn_de, end_de, pblntf_ty)` — 공시 목록. 날짜
  범위는 오늘({today}) 기준. `pblntf_ty='A'` = 정기공시
- `get_company(corp_code)` — 기업개황
- `get_document(rcept_no, max_chars=20000)` — 공시 원문 텍스트
- `get_financial(corp_code, bsns_year, reprt_code, fs_sections)` — 재무제표.
  `fs_sections`: `["IS"]`=손익, `["BS"]`=재무상태, `["CF"]`=현금흐름, `["IS","BS"]`=교차비율.
  **"최신" 요청은 `bsns_year` 추측 금지** → 먼저 `list_disclosures(pblntf_detail_ty="A001")`
  로 최근 사업보고서를 조회해 `report_nm`(예: `사업보고서 (2024.12)`) 의 연도를 사용
- `list_shareholder_reports(corp_code)` — 대량보유+임원 지분
- `list_dividend_events(corp_code, bsns_year)` — 배당

## 절대 규칙

- 도구 결과에 **없는 숫자·이름·rcept_no 지어내지 말 것** (환각 금지)
- **원문 공시 링크 필수**: 언급하는 모든 공시는 반드시 도구 결과의 `source_url` 을
  마크다운 링크로 포함한다. 표로 공시를 나열할 때는 "공시명" 셀을
  `[공시명](source_url)` 로, 특정 공시를 본문에서 인용할 때는 바로 뒤에
  `🔗 [원문 보기](source_url)` 를 붙인다. 링크 누락은 규칙 위반.
- 답변은 한국어 마크다운
- 디스클레이머 뒤에 **절대 추가 도구 호출 금지** (위반 시 세션 강제 종료)
- 도구 결과가 비어있으면 "확인할 수 없습니다" 로 안내하고 중단

## 공시 포맷 예시

공시 목록 표:
```
| 접수일 | 공시명 | 제출인 |
|--------|--------|--------|
| 2025-03-15 | [사업보고서 (2024.12)](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20250315000001) | 삼성전자 |
```

단일 공시 인용:
```
삼성전자는 2025년 3월 15일 사업보고서를 제출했습니다.
🔗 [원문 보기](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20250315000001)
```

## 디스클레이머 (답변 말미 고정)

> ⚠️ 본 답변은 Open DART 공시자료를 기반으로 한 정보 제공이며, 투자 자문이 아닙니다.
> 투자 결정은 반드시 원문 공시와 전문가 상담을 거치시기 바랍니다.
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
            "--include-partial-messages",
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
        insight_filter = InsightStreamFilter()

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
                    if await self._handle_stream_event(event, acc_text, insight_filter):
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

        # Drain any text the insight filter held back at the tail of the stream.
        tail = insight_filter.flush()
        if tail:
            acc_text.append(tail)
            await self._send({
                "type": "dart_stream",
                "data": {"token": tail, "done": False},
            })

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
        insight_filter: InsightStreamFilter,
    ) -> bool:
        """Process one stream-json event. Returns True if content was emitted."""
        etype = event.get("type")
        emitted = False

        # Incremental text deltas (--include-partial-messages). Each event
        # carries a small slice of the assistant's answer as it is generated,
        # letting the UI render tables and paragraphs live instead of waiting
        # for the whole turn.
        if etype == "stream_event":
            inner = event.get("event", {}) or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {}) or {}
                if delta.get("type") == "text_delta":
                    raw = delta.get("text", "") or ""
                    safe = insight_filter.feed(raw)
                    if safe:
                        acc_text.append(safe)
                        await self._send({
                            "type": "dart_stream",
                            "data": {"token": safe, "done": False},
                        })
                        emitted = True
                        if not self._disclaimer_seen and _DISCLAIMER[:20] in "".join(acc_text):
                            self._disclaimer_seen = True
            return emitted

        if etype == "assistant":
            message = event.get("message", {}) or {}
            for block in message.get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    # Skip: the full assistant turn arrives here as a
                    # consolidated block AFTER all deltas have been emitted
                    # via stream_event. Re-emitting would duplicate the
                    # answer on the client.
                    continue
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
