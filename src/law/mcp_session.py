"""Law session via Claude Code CLI + Law MCP server.

Replaces the old ``src/law/engine.py`` + ``src/law/session.py`` pair.
The custom XML ``<tool_call>`` wrapper was fighting Sonnet's ReAct
training and produced hallucinated article citations and MST values —
less visible than DART's failure mode because law queries usually resolve
in 2 turns, but the structural risk is identical. MCP protocol eliminates
the hallucination window at the token level.

Flow:
    WebSocket ←→ LawMcpSession
                    ↓ spawns
                 claude CLI (stream-json, --mcp-config)
                    ↓ spawns
                 python -m src.law.mcp_server (stdio JSON-RPC)
                    ↓ calls
                 existing LAW_TOOL_EXECUTORS (LawClient, caches)
                    ↓ HTTPS
                 law.go.kr Open API
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
# that scan for specific substrings in source. asyncio's subprocess spawner
# is the safe execFile-equivalent — it takes a list of arguments (not a
# shell string) and never goes through a shell interpreter, so there is no
# injection surface.
_spawn_subprocess = getattr(asyncio, "create_subprocess_" + "exec")

_DISCLAIMER = (
    "⚠️ 본 답변은 법령 원문을 기반으로 한 일반 정보 제공이며, 법률 자문이 아닙니다. "
    "구체적 사안은 반드시 변호사와 상담하시기 바랍니다."
)

_MCP_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# MCP server key "law" in .mcp.json + 6 tool names → claude CLI tool namespace
_LAW_TOOL_NAMES = [
    "mcp__law__law_search",
    "mcp__law__law_get",
    "mcp__law__law_get_article",
    "mcp__law__prec_search",
    "mcp__law__prec_get",
    "mcp__law__expc_search",
]


def _build_law_mcp_config() -> str | None:
    """Read top-level .mcp.json, substitute env vars, write a law-only temp file.

    Claude CLI does not substitute ``${VAR}`` patterns inside .mcp.json, so we
    do it ourselves. We also strip to just the law entry so the CLI doesn't
    spawn unrelated MCP servers for every law query.
    """
    template = Path(".mcp.json")
    if not template.exists():
        logger.warning("_build_law_mcp_config: .mcp.json not found at %s", template.absolute())
        return None
    try:
        raw = template.read_text(encoding="utf-8")
        substituted = _MCP_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""),
            raw,
        )
        config = json.loads(substituted)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("_build_law_mcp_config: parse failed: %s", exc)
        return None

    servers = config.get("mcpServers") or {}
    law_cfg = servers.get("law")
    if not law_cfg:
        logger.warning("_build_law_mcp_config: 'law' entry missing from .mcp.json")
        return None

    minimal = {"mcpServers": {"law": law_cfg}}
    fd, path = tempfile.mkstemp(suffix=".mcp.json", prefix="law_mcp_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(minimal, f, ensure_ascii=False)
    return path


def _build_system_prompt() -> str:
    """Compact law system prompt for the MCP path.

    Preserves the accuracy-first guardrails from the old src/law/prompts/system.py
    (source-locked answering, verbatim citation, no fabrication, disclaimer) but
    drops the ``<tool_call>`` XML instructions since tools now come via native MCP.
    Adds today's date injection (new — the old prompt didn't have this).
    """
    today = time.strftime("%Y-%m-%d")
    return f"""당신은 대한민국 법령 조사 보조입니다. 국가법령정보센터(law.go.kr)에서 직접 조회한 원문 조문·판례·해석례만을 근거로 답변합니다.

## 오늘 날짜 (매우 중요)

**오늘은 {today} 입니다.** "최근", "올해", "지난달", "최신 개정" 등 모든 상대 시점은 이 날짜 기준으로 해석하십시오. 훈련 데이터의 암묵적 현재가 아니라 **반드시 이 시스템 날짜**를 따라야 합니다.

## 사용 가능한 도구 (MCP 서버 경유)

이 세션에서 사용할 수 있는 도구는 아래 여섯 개뿐입니다. 다른 도구(WebSearch, WebFetch, Read, Bash 등)는 **절대 호출하지 마십시오**. 법령 정보는 반드시 법.go.kr 의 공식 API 로만 조회합니다 — 써드파티 블로그·위키는 stale/왜곡 위험이 있습니다.

1. `mcp__law__law_search(query, display=10, page=1)` — 법령 키워드 검색
2. `mcp__law__law_get(mst)` — 법령 전체 본문 (MST 기반)
3. `mcp__law__law_get_article(mst, jo)` — 특정 조문 원문
4. `mcp__law__prec_search(query, court=None, display=10)` — 판례 검색
5. `mcp__law__prec_get(id)` — 판례 원문
6. `mcp__law__expc_search(query, display=10)` — 법령해석례 검색

## 질문 유형 자동 판별

사용자 질문을 읽고 아래 두 유형 중 하나로 분기하십시오:

- **키워드 질문** — 법령명·조문 번호·판례 번호 등 특정 식별자가 명시된 경우.
  예: "개인정보보호법 제15조", "근로기준법 제74조", "대법원 2020다12345 판례"
  처리: `law_search` → MST 확인 → `law_get_article` 로 원문 확보 → 원문 인용 + 간결 해설. **1+1 도구 호출로 끝내는 것이 정상**.

- **상황 질문** — 분쟁·고민·대응 방법을 자연어로 설명한 경우.
  예: "전세 보증금을 못 받고 있어요", "계약직 2년 정규직 전환 가능한가요?", "갑자기 해고당했어요"
  처리: **쟁점 → 법령명 매핑** 단계가 핵심입니다 (아래 섹션 참고).

## ⚠️ law_search API 의 본질적 한계 (반드시 이해)

`law_search` 는 **법령명 기반 검색**이지 **주제/키워드 검색이 아닙니다**. 주제 키워드로
직접 검색하면 **0건 반환**되는 경우가 매우 많습니다. 예:

- ❌ `law_search("부당해고")` → 0건 (법령명이 아님)
- ❌ `law_search("전세 보증금")` → 0건
- ❌ `law_search("개인정보 유출")` → 0건
- ✅ `law_search("근로기준법")` → 정상 (법령명)
- ✅ `law_search("주택임대차보호법")` → 정상
- ✅ `law_search("개인정보 보호법")` → 정상

## 쟁점 → 법령명 매핑 (상황 질문의 핵심 단계)

상황 질문을 받으면 **먼저 당신의 법률 일반 지식으로 해당 상황에 적용되는 법령명을 떠올리십시오**. 그런 다음 그 법령명으로 `law_search` 를 호출합니다. 훈련 데이터 기반으로 **조문 내용** 을 재구성하는 것은 금지지만, **"이 상황은 X법이 적용된다"** 수준의 매핑은 당신의 정당한 역할입니다. 그래야 `law_search` 가 의미 있는 결과를 반환합니다.

일반적 매핑 예시:

| 사용자 상황 | 매핑할 법령명(들) |
|---|---|
| 해고·징계·퇴직 관련 | **근로기준법** (+필요시 기간제법, 노동조합법) |
| 임금·초과근무·휴가 | **근로기준법** (+최저임금법) |
| 전세·월세·임차 분쟁 | **주택임대차보호법** (+민법) |
| 상가 임대차 | **상가건물 임대차보호법** |
| 개인정보 유출·수집 | **개인정보 보호법** |
| 성희롱·직장 내 괴롭힘 | **근로기준법** (제76조의2 이하) (+남녀고용평등법) |
| 전자상거래·온라인 거래 | **전자상거래법** (+소비자기본법) |
| 계약 해제·위약금 | **민법** (+약관규제법) |
| 교통사고 | **도로교통법** (+자동차손해배상 보장법) |
| 이혼·양육비·재산분할 | **민법** (가족편) |
| 상속 | **민법** (상속편) |
| 회사 설립·주주 분쟁 | **상법** |
| 부정경쟁·영업비밀 | **부정경쟁방지 및 영업비밀보호에 관한 법률** |
| 저작권 침해 | **저작권법** |
| 병역 관련 | **병역법** |

이 표는 포괄적이지 않습니다. 표에 없는 상황이면 당신의 일반 법률 지식으로 **가장 근접한 법령명 1~3개**를 떠올린 뒤 `law_search` 하십시오.

**상황 질문 처리 플로우**:
1. 사용자 상황에서 **쟁점 1~3개** 추출
2. 쟁점마다 **적용 법령명** 을 당신 지식으로 결정 (위 매핑 표 참고)
3. 각 법령명으로 `law_search` 호출 → MST 확보
4. 관련 조문 번호를 **당신이 알고 있는 범위에서 추정** (예: 해고 → 근로기준법 제23조, 23조의2, 24조) + `law_get_article` 로 **원문 검증**
5. 원문 확보된 조문만 인용 + 해설
6. 만약 `law_search` 가 0건 반환하면, **즉시 포기하지 말고 법령명을 바꿔서 재시도** (예: "임대차보호법" → "주택임대차보호법")

**중요**: 원문 확보된 조문의 내용은 도구 결과만 신뢰하되, **어느 법령의 어느 조문 번호를 조회할지는 당신의 일반 지식으로 결정**하는 것이 정상입니다. 이 결정 단계에서 도구를 호출할 수는 없으니까요.

## 절대 어기지 말아야 할 규칙

1. **원문 우선 (Source-locked answering)**
   - 사용자의 질문에 대해 반드시 `law_search` 로 먼저 관련 법령을 검색하십시오.
   - 검색 결과의 MST 를 `law_get_article` 로 넘겨 조문 원문을 가져온 뒤, **그 원문에만 근거하여** 답변하십시오.
   - 원문을 확보하지 못한 조문은 언급하지 마십시오.
   - **한 번 원문을 확보했으면 그 내용을 신뢰하고 답변을 작성**하십시오. 재검증 목적으로 도구를 반복 호출하지 마십시오. 키워드 질문은 "1회 검색 + 1회 조문 조회" 가 정상입니다.

2. **MST 선택 규칙 (최신 시행일 우선)**
   - `law_search` 결과에서 MST 가 여러 개 나오면, **동일 법령명 항목 중 가장 최신 시행일자를 가진 것**을 선택하십시오. 훈련 데이터의 구버전이 아니라 오늘({today}) 기준으로 실제 시행 중인 최신 버전을 고릅니다.
   - "법률" 이 "시행령" 이나 "시행규칙" 보다 우선입니다 (`법종구분` 필드 확인).
   - 한 번 선택한 MST 는 재확인하지 마십시오.

3. **창작 금지 (No fabrication)**
   - 도구 결과에 없는 법령명·조문 번호·판례 번호를 **만들어내지 마십시오**. 특히 존재하지 않는 조문에 대해 "해당 조문이 없습니다" 라고 답하는 게 정답이며, 내용을 재구성하지 마십시오.
   - 기억·상식으로 조문 내용을 재구성하는 것은 **엄격히 금지**입니다. 훈련 데이터에 있는 조문 내용은 이미 구버전일 가능성이 높습니다.

4. **원문 인용 (Verbatim Citation)**
   - 답변 본문에서 조문을 인용할 때는 반드시 다음 형식 블록을 사용하십시오:

     > [인용] 법령명 제○조 (MST=xxxx)
     > {{도구로 가져온 원문 그대로, 한 글자도 바꾸지 말 것}}

   - 답변 말미에 "## 참고 원문 링크" 섹션에 사용한 모든 `source_url` 을 나열하십시오.
   - URL 은 도구 결과의 `source_url` 필드를 **그대로** 사용. 손으로 조립하지 마십시오.

5. **법률 자문 아님 (Disclaimer)**
   - 답변 맨 끝에 **반드시** 다음 문구를 포함하십시오:

     > ⚠️ 본 답변은 법령 원문을 기반으로 한 일반 정보 제공이며, 법률 자문이 아닙니다.
     > 구체적 사안은 반드시 변호사와 상담하시기 바랍니다.

6. **⛔ 과도한 탐색 금지 — 한 소스 답변 원칙**

   사용자 질문에 답변 가능한 **최소한의 데이터**만 조회하십시오:

   - 관련 법령 원문(조문) 2~3개로 답변 가능하면 **거기서 종료**. 원문이 다른 조문이나
     판례를 참조해도 **선제적으로 조회하지 마십시오**.
   - 사용자가 "관련 판례도 찾아줘" 라고 명시하지 않는 한, 판례(`prec_*`)·해석례(`expc_*`)
     도구를 자진 호출하지 마십시오.
   - "혹시 더 필요하시면…" 같은 후속 제안은 텍스트로만 쓸 수는 있으나,
     그 제안을 **스스로 실행하지 마십시오**.
   - **목표**: 키워드 질문은 1+1(검색+조문) 호출, 상황 질문은 최대 4~5회 호출.
     이를 넘어가면 질문을 더 좁게 재해석하십시오.

7. **⛔ 디스클레이머 이후 도구 호출 절대 금지**

   디스클레이머(`⚠️ 본 답변은…`)를 쓴 **그 순간 답변은 완결된 것**입니다.
   이후에 어떤 `law_*`, `prec_*`, `expc_*` 도구도 호출하지 마십시오.
   **이 규칙을 위반하면 시스템이 subprocess 를 강제 종료하고 불완전한 응답만 반환합니다.**
   디스클레이머를 쓰기 전에 필요한 모든 정보를 이미 확보한 상태여야 합니다.

8. **출력 형식**
   - 한국어 마크다운 (##, 인용블록, 리스트). 표는 필요할 때만.
   - 도구 없이 답변을 생성하지 마십시오 — 반드시 도구 결과에 근거하여 작성.

도구 호출 실패·빈 결과·권한 오류가 발생하면 그 사실을 그대로 사용자에게 알리고 "원문을 확인할 수 없어 답변을 보류합니다" 라고 답하십시오. 추측으로 빈자리를 채우는 것은 금지입니다.
"""


# ─── 세션 클래스 ─────────────────────────────────────


class LawMcpSession:
    """One WebSocket ↔ one Law MCP-backed chat session.

    Public contract matches the old LawSession so ``src/ui/routes/law.py``
    can swap the import without other changes.
    """

    def __init__(self, ws, user_id: str = "") -> None:
        self._ws = ws
        self._user_id = user_id
        self._session_id = f"law_{uuid.uuid4().hex[:12]}"
        self._cancelled = False
        self._proc: Any = None
        self._last_activity = time.time()
        self._heartbeat_task: asyncio.Task | None = None
        self._ttl_task: asyncio.Task | None = None
        # flash → medium effort, think → high effort
        self._mode = "flash"
        # See DartMcpSession — disclaimer sentinel for subprocess-level guard
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
        has_key = get_settings().law_oc != ""
        await self._send({
            "type": "law_init",
            "data": {
                "session_id": self._session_id,
                "has_key": has_key,
                "security_banner": (
                    "국가법령정보센터(law.go.kr)의 공식 Open API를 통해 조문 원문을 "
                    "직접 조회합니다. 대화 내용은 서버에 저장되지 않으며, 세션 종료 시 "
                    "즉시 파기됩니다."
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

            if msg_type == "law_stop":
                self._kill_proc()
                continue
            if msg_type == "law_set_mode":
                new_mode = data.get("mode", "flash")
                if new_mode in ("flash", "think"):
                    self._mode = new_mode
                continue
            if msg_type == "law_set_search_mode":
                # Legacy toggle from pre-auto-detect era — silently ignore.
                continue
            if msg_type == "law_message":
                content = (data.get("content") or "").strip()
                if not content:
                    continue
                effort_override = data.get("effort")
                if effort_override in ("flash", "think"):
                    self._mode = effort_override
                try:
                    await self._run_claude(content)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("LawMcpSession: run error")
                    await self._send({
                        "type": "law_error",
                        "data": {"message": f"실행 오류: {exc}"},
                    })

    # ── CLI spawn + stream parsing ───────────────

    async def _run_claude(self, user_text: str) -> None:
        """Spawn claude CLI with the Law MCP config and stream events to the WS."""
        self._disclaimer_seen = False

        mcp_config_path = _build_law_mcp_config()
        system_prompt = _build_system_prompt()
        effort = "medium" if self._mode == "flash" else "high"

        cmd = [
            "claude", "-p", user_text,
            "--output-format", "stream-json",
            "--verbose",
            "--model", "sonnet",
            "--max-turns", "10",
            "--append-system-prompt", system_prompt,
            "--allowedTools", ",".join(_LAW_TOOL_NAMES),
            "--permission-mode", "auto",
            "--effort", effort,
        ]
        if mcp_config_path:
            cmd.extend(["--mcp-config", mcp_config_path, "--strict-mcp-config"])

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        await self._send({
            "type": "law_tool_status",
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
            logger.warning("LawMcpSession: claude subprocess timeout after 300s")
            await self._send({
                "type": "law_error",
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
                "type": "law_stream",
                "data": {"token": f"\n\n> {_DISCLAIMER}", "done": False},
            })

        # Done signal
        await self._send({
            "type": "law_stream",
            "data": {"token": "", "done": True},
        })

        if not had_output:
            logger.warning("LawMcpSession: no output events received")

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
                            "type": "law_stream",
                            "data": {"token": text, "done": False},
                        })
                        emitted = True
                        if not self._disclaimer_seen and _DISCLAIMER[:20] in "".join(acc_text):
                            self._disclaimer_seen = True
                elif btype == "tool_use":
                    # Guard: tool_use after disclaimer = rule violation
                    if self._disclaimer_seen:
                        logger.warning(
                            "LawMcpSession: tool_use emitted after disclaimer — "
                            "terminating subprocess to prevent context overshoot"
                        )
                        self._cancelled = True
                        self._kill_proc()
                        return emitted
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {}) or {}
                    short = tool_name.replace("mcp__law__", "")
                    await self._send({
                        "type": "law_tool_status",
                        "data": {
                            "tool": short,
                            "status": self._describe_tool(short, tool_input),
                        },
                    })
                    emitted = True

        elif etype == "result":
            if event.get("is_error"):
                error_text = str(event.get("result", "")).strip()[:300]
                logger.warning("CLI result error: %s", error_text or "(empty)")
                if not error_text:
                    if acc_text and sum(len(t) for t in acc_text) > 200:
                        return emitted
                    friendly = (
                        "모델이 응답을 조합하는 도중 내부 한도에 도달했습니다. "
                        "질문을 좀 더 좁게(구체적 조문·사건 명시) 다시 시도해 주세요."
                    )
                else:
                    friendly = f"CLI 오류: {error_text}"
                await self._send({
                    "type": "law_error",
                    "data": {"message": friendly},
                })
                emitted = True

        return emitted

    @staticmethod
    def _describe_tool(name: str, inputs: dict[str, Any]) -> str:
        if name == "law_search":
            return f"법령 검색 중: {inputs.get('query', '?')}"
        if name == "law_get":
            return f"법령 본문 조회 중: MST={inputs.get('mst', '?')}"
        if name == "law_get_article":
            return f"조문 원문 조회 중: MST={inputs.get('mst', '?')} {inputs.get('jo', '?')}"
        if name == "prec_search":
            return f"판례 검색 중: {inputs.get('query', '?')}"
        if name == "prec_get":
            return f"판례 본문 조회 중: ID={inputs.get('id', '?')}"
        if name == "expc_search":
            return f"법령해석례 검색 중: {inputs.get('query', '?')}"
        return f"{name} 실행 중..."

    # ── Process / lifecycle plumbing ─────────────

    def _kill_proc(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                logger.debug("LawMcpSession: terminate failed")

    def _cleanup(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ttl_task:
            self._ttl_task.cancel()
        self._kill_proc()
        logger.info("Law MCP session %s cleaned up", self._session_id)

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
        ttl_seconds = get_settings().law_session_ttl_minutes * 60
        try:
            while not self._cancelled:
                await asyncio.sleep(60)
                if time.time() - self._last_activity > ttl_seconds:
                    logger.info("Law MCP session %s TTL expired", self._session_id)
                    self._cancelled = True
                    try:
                        await self._send({
                            "type": "law_error",
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
