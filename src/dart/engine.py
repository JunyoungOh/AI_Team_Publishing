"""DartEngine — CLI-bridge tool loop for the DART (전자공시) mode.

Mirrors ``src/law/engine.py`` structurally but swaps law-specific guards
and descriptions for DART ones. Seven custom tools + a verbatim guard
that flags rcept_no references the LLM didn't actually fetch.
"""
from __future__ import annotations

import json
import logging
import re
from functools import partial
from typing import Any

from src.config.settings import get_settings
from src.dart.prompts.system import build_system_prompt
from src.dart.tools import DART_TOOL_EXECUTORS, DART_TOOL_SCHEMAS, make_session_context
from src.utils.bridge_factory import get_bridge

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "⚠️ 본 답변은 Open DART 공시자료를 기반으로 한 정보 제공이며, 투자 자문이 아닙니다. "
    "투자 결정은 반드시 원문 공시와 전문가 상담을 거치시기 바랍니다."
)

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# DART 공시접수번호 — 14자리 숫자
_RCEPT_NO_RE = re.compile(r"rcept_no\s*[=:]\s*(\d{14})")

# LLM이 system 프롬프트를 어기고 source_url을 손으로 조립할 때 자주 하는 실수:
# 응답 JSON의 `"rcept_no"` 필드명을 그대로 복사해서 URL 파라미터로 쓰는데,
# DART 뷰어가 실제로 받는 파라미터 이름은 `rcpNo`(camelCase). snake_case인
# `rcept_no=xxxxxxxxxxxxxx`로 만들어진 URL은 DART가 "거부" 페이지를 반환한다.
# 이 새니타이저는 DART URL 쿼리 위치(`?` 또는 `&` 직후)의 14자리 rcept_no를
# rcpNo로 재작성한다. 답변 본문 자연어 속의 "rcept_no=..." 같은 메타 언급에는
# 건드리지 않도록 `?`/`&` 접두 + 14자리 숫자 뒤매칭으로 제한.
_DART_URL_RCEPT_FIX_RE = re.compile(r"([?&])rcept_no=(\d{14})")

# Medium-effort Sonnet handles 4-turn DART loops without bail-outs in
# our test runs. Past 5 turns the user is better off reframing.
_MAX_TURNS = 5

# Keys that give away a raw Open DART JSON blob when the LLM echoes tool
# output into its response text. Used by ``_strip_json_echo``.
_JSON_ECHO_KEYS = (
    '"corp_code"',
    '"rcept_no"',
    '"stock_code"',
    '"corp_name"',
    '"sj_div"',
    '"sj_nm"',
    '"account_nm"',
    '"thstrm_amount"',
    '"frmtrm_amount"',
    '"source_url"',
    '"fetched_at"',
)

# Stray Claude Code tool_use markers that leak through when the CLI fell
# back to returning raw stream-json.
_TOOLU_MARKER_RE = re.compile(r"toolu_[A-Za-z0-9]+")

# Orphan tool-result section labels the LLM sometimes writes as narration
# headers above a JSON dump. After ``_strip_json_echo`` removes the JSON,
# these labels are left behind.
_TOOL_RESULT_LABEL_RE = re.compile(
    r"^\s*\[\s*(?:dart[_ ]?(?:search|result|disclosure|company|document|financial)|"
    r"tool[_ ]?result(?:[:\s]+\w+)?|"
    r"(?:resolve_corp_code|list_disclosures|get_company|get_document|get_financial|"
    r"list_shareholder_reports|list_dividend_events)\s+result)"
    r"[\s\w]*\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _build_tool_instructions(schemas: dict[str, dict[str, Any]]) -> str:
    lines = [
        "You have access to the following custom tools. To use a tool, respond with a "
        "<tool_call> block containing JSON with 'name' and 'input' keys.\n"
        "Example: <tool_call>{\"name\": \"resolve_corp_code\", \"input\": {\"query\": \"삼성전자\"}}</tool_call>\n"
        "\nYou can call MULTIPLE tools in a single response by including multiple <tool_call> blocks.\n"
        "\nAvailable tools:"
    ]
    for name, schema in schemas.items():
        desc = schema.get("description", "")
        input_schema = json.dumps(schema.get("input_schema", {}), ensure_ascii=False)
        lines.append(f"\n## {name}\n{desc}\nInput schema: {input_schema}")
    return "\n".join(lines)


def _parse_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in _TOOL_CALL_RE.finditer(text):
        try:
            obj = json.loads(match.group(1))
            if "name" in obj:
                calls.append(obj)
        except json.JSONDecodeError:
            logger.debug("DartEngine: failed to parse tool_call JSON: %s", match.group(1)[:200])
    return calls


def _strip_tool_calls(text: str) -> str:
    return _TOOL_CALL_RE.sub("", text).strip()


def _find_balanced_end(text: str, start: int) -> int:
    """Return the index of the matching ``]``/``}`` for the bracket at *start*."""
    if start >= len(text):
        return -1
    open_ch = text[start]
    if open_ch == "[":
        close_ch = "]"
    elif open_ch == "{":
        close_ch = "}"
    else:
        return -1
    depth = 0
    in_string = False
    escape = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if escape:
            escape = False
        elif ch == "\\" and in_string:
            escape = True
        elif in_string:
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _strip_json_echo(text: str) -> str:
    """Remove JSON-shaped blobs that the LLM copied from tool results."""
    if not text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "[{":
            end = _find_balanced_end(text, i)
            if end > i:
                block = text[i : end + 1]
                if any(key in block for key in _JSON_ECHO_KEYS):
                    i = end + 1
                    if i < n and text[i] == "\n":
                        i += 1
                    continue
        out.append(ch)
        i += 1
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _strip_toolu_markers(text: str) -> str:
    if not text or "toolu_" not in text:
        return text
    cleaned = _TOOLU_MARKER_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _strip_tool_result_labels(text: str) -> str:
    if not text:
        return text
    cleaned = _TOOL_RESULT_LABEL_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _sanitise_dart_urls(text: str) -> str:
    """Rewrite LLM-fabricated DART URLs with the wrong parameter name.

    Specifically: `?rcept_no=NNNNNNNNNNNNNN` → `?rcpNo=NNNNNNNNNNNNNN` (and
    same for `&`). Only touches query positions so natural-language mentions
    of "rcept_no=..." elsewhere in the answer are untouched.
    """
    if not text or "rcept_no=" not in text or "dart.fss.or.kr" not in text:
        return text
    return _DART_URL_RCEPT_FIX_RE.sub(r"\1rcpNo=\2", text)


def _parse_cli_response(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse CLI's raw output into (clean_text, native_tool_calls).

    Handles plain text and raw stream-json NDJSON fallback. Same logic
    as ``src/law/engine.py:_parse_cli_response``.
    """
    if not raw:
        return "", []
    stripped = raw.lstrip()
    if not stripped.startswith("{") or '"type"' not in raw:
        return raw, []
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    saw_any_event = False
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_any_event = True
        if event.get("type") != "assistant":
            continue
        message = event.get("message", {}) or {}
        for block in message.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    texts.append(txt)
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "name": block.get("name", ""),
                        "input": block.get("input", {}) or {},
                    }
                )
    if not saw_any_event:
        return raw, []
    return "\n".join(texts), tool_calls


class DartEngine:
    """One chat session against the DART-assistant prompt."""

    def __init__(self, ws: Any) -> None:
        self._settings = get_settings()
        self._bridge = get_bridge()
        self._ws = ws
        self._system_prompt = build_system_prompt()
        self._tool_instructions = _build_tool_instructions(DART_TOOL_SCHEMAS)
        self._messages: list[dict[str, Any]] = []
        self._cancelled = False
        # Two-tier effort toggle mirroring the law tab UX:
        # flash → medium (표준, 기본), think → high (심층 분석). 법령 탭과 달리
        # 숫자·재무제표가 주 컨텐츠라 "fast" 경로도 low가 아닌 medium이어야
        # 누락/오류 위험이 낮음.
        self._mode = "flash"

        ctx = make_session_context()
        self._ctx = ctx
        self._tool_executors = {
            name: partial(fn, ctx) for name, fn in DART_TOOL_EXECUTORS.items()
        }
        self._best_display_text: str = ""

    # -- Public API -----------------------------------------------

    def cancel(self) -> None:
        self._cancelled = True

    def set_mode(self, mode: str) -> None:
        """Toggle flash (medium effort) / think (high effort)."""
        if mode in ("flash", "think"):
            self._mode = mode

    async def send_message(self, content: str) -> None:
        self._cancelled = False
        self._best_display_text = ""
        self._messages.append({"role": "user", "content": content})
        await self._run_loop()

    # -- Internals ------------------------------------------------

    def _effort(self) -> str:
        return "high" if self._mode == "think" else "medium"

    async def _run_loop(self) -> None:
        full_system = f"{self._system_prompt}\n\n{self._tool_instructions}"
        turns = 0

        while turns < _MAX_TURNS and not self._cancelled:
            turns += 1
            user_message = self._build_conversation_text()

            await self._send({
                "type": "dart_tool_status",
                "data": {"tool": "", "status": f"분석 중... (턴 {turns}/{_MAX_TURNS})"},
            })

            try:
                response_text = await self._bridge.raw_query(
                    system_prompt=full_system,
                    user_message=user_message,
                    model="sonnet",
                    # WebSearch/WebFetch are decoy native tools — prevents
                    # Sonnet 4.6 from defaulting to native tool_use protocol
                    # for our custom tools when allowed_tools is empty.
                    allowed_tools=["WebSearch", "WebFetch"],
                    max_turns=5,
                    timeout=240,
                    effort=self._effort(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("DartEngine bridge error")
                if self._best_display_text:
                    final_text = self._finalise(self._best_display_text)
                    tail = final_text[len(self._best_display_text):]
                    if tail:
                        await self._send({
                            "type": "dart_stream",
                            "data": {"token": tail, "done": False},
                        })
                    await self._flush_pending_citations()
                    await self._send({
                        "type": "dart_stream",
                        "data": {
                            "token": f"\n\n— *(일부 분석 단계에서 오류가 발생했지만, 확보된 답변을 반환합니다: {type(exc).__name__})*",
                            "done": True,
                        },
                    })
                else:
                    await self._send({
                        "type": "dart_error",
                        "data": {
                            "message": (
                                f"모델 호출 실패: {exc}. "
                                "질문을 좀 더 짧게 다시 입력해 주세요."
                            ),
                        },
                    })
                return

            if self._cancelled:
                return

            cli_text, native_tool_calls = _parse_cli_response(response_text)
            if native_tool_calls:
                logger.info(
                    "DartEngine: promoted %d native tool_use call(s) from stream-json",
                    len(native_tool_calls),
                )
                response_text = cli_text
            display_text = _strip_tool_calls(response_text)
            display_text = _strip_json_echo(display_text)
            display_text = _strip_tool_result_labels(display_text)
            display_text = _strip_toolu_markers(display_text)
            display_text = _sanitise_dart_urls(display_text)
            if display_text:
                await self._send({
                    "type": "dart_stream",
                    "data": {"token": display_text, "done": False},
                })
                if len(display_text) > len(self._best_display_text):
                    self._best_display_text = display_text

            self._messages.append({"role": "assistant", "content": response_text})

            xml_tool_calls = _parse_tool_calls(response_text)
            tool_calls = xml_tool_calls + native_tool_calls
            if tool_calls and self._looks_final(display_text):
                logger.info(
                    "DartEngine: early termination — answer len=%d, %d tool_calls ignored",
                    len(display_text), len(tool_calls),
                )
                tool_calls = []

            if not tool_calls:
                final_text = self._finalise(display_text)
                if final_text != display_text:
                    await self._send({
                        "type": "dart_stream",
                        "data": {"token": f"\n\n{final_text[len(display_text):]}", "done": False},
                    })
                await self._flush_pending_citations()
                await self._send({
                    "type": "dart_stream",
                    "data": {"token": "", "done": True},
                })
                return

            # Execute tools and feed results back.
            tool_results: list[str] = []
            for call in tool_calls:
                name = call.get("name", "")
                inputs = call.get("input", {}) or {}
                await self._send({
                    "type": "dart_tool_status",
                    "data": {"tool": name, "status": self._describe(name, inputs)},
                })
                result = await self._execute_tool(name, inputs)
                tool_results.append(
                    f"<observation tool=\"{name}\">\n{result[:8000]}\n</observation>"
                )

            await self._flush_pending_citations()

            self._messages.append({
                "role": "user",
                "content": (
                    "<tool-observations>\n"
                    + "\n".join(tool_results)
                    + "\n</tool-observations>\n\n"
                    "위 관찰 결과를 바탕으로 사용자에게 자연어로 답변을 이어가십시오. "
                    "확보되지 않은 공시는 언급하지 마시고, "
                    "관찰 결과의 원문(JSON, XML, 라벨 등)은 절대 응답 본문에 복사하지 마십시오."
                ),
            })
            self._prune_history()

        if not self._cancelled:
            if self._best_display_text:
                final_text = self._finalise(self._best_display_text)
                tail = final_text[len(self._best_display_text):]
                if tail:
                    await self._send({
                        "type": "dart_stream",
                        "data": {"token": tail, "done": False},
                    })
                await self._flush_pending_citations()
                await self._send({
                    "type": "dart_stream",
                    "data": {
                        "token": "\n\n— *(최대 분석 횟수 도달 — 가장 완성된 답변을 반환합니다)*",
                        "done": True,
                    },
                })
            else:
                await self._send({
                    "type": "dart_stream",
                    "data": {
                        "token": "\n\n[DART] 최대 실행 횟수에 도달했습니다. 질문을 좀 더 구체적으로 다시 해주세요.",
                        "done": True,
                    },
                })

    async def _execute_tool(self, name: str, inputs: dict[str, Any]) -> str:
        executor = self._tool_executors.get(name)
        if executor is None:
            return f"Error: unknown tool '{name}'"
        try:
            return await executor(**inputs)
        except TypeError as exc:
            return f"Error: invalid input for {name} — {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("DART tool execution error (%s)", name)
            return f"Error executing {name}: {type(exc).__name__}: {exc}"

    def _build_conversation_text(self) -> str:
        parts: list[str] = []
        for msg in self._messages:
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                parts.append(f"[User]: {content}")
            else:
                parts.append(f"[Assistant]: {content}")
        return "\n\n".join(parts)

    def _prune_history(self) -> None:
        tr_indices = [
            i for i, m in enumerate(self._messages)
            if m["role"] == "user"
            and isinstance(m["content"], str)
            and m["content"].startswith("<tool-observations>")
        ]
        if len(tr_indices) <= 1:
            return
        for idx in tr_indices[:-1]:
            content = self._messages[idx]["content"]
            if isinstance(content, str) and len(content) > 500:
                self._messages[idx]["content"] = content[:300] + "\n...[이전 결과 요약됨]"

    # -- Finalisation ---------------------------------------------

    @staticmethod
    def _looks_final(text: str) -> bool:
        """Disclaimer footer == unambiguous 'done' signal."""
        if not text:
            return False
        return _DISCLAIMER[:20] in text

    def _finalise(self, text: str) -> str:
        guarded = self._verbatim_guard(text)
        guarded = _sanitise_dart_urls(guarded)
        if _DISCLAIMER[:20] not in guarded:
            guarded = guarded.rstrip() + f"\n\n> {_DISCLAIMER}"
        return guarded

    def _verbatim_guard(self, text: str) -> str:
        """Flag rcept_no references that weren't backed by a get_document call.

        The LLM should only quote original text from disclosures whose
        rcept_no is in ``verified_disclosures`` (i.e. get_document was
        actually called). Unverified rcept_no mentions get an inline warning.
        """
        verified = self._ctx["verified_disclosures"]
        rcept_refs = _RCEPT_NO_RE.findall(text)
        if not rcept_refs:
            return text
        unverified = [r for r in rcept_refs if r not in verified]
        if unverified:
            unique = sorted(set(unverified))
            return text + (
                f"\n\n> ⚠️ 원문을 조회하지 않은 rcept_no: {', '.join(unique)} — "
                "DART 뷰어에서 직접 확인하시기 바랍니다."
            )
        return text

    async def _flush_pending_citations(self) -> None:
        """Drain the per-turn citation queue.

        DART citations (rcept_no + source_url) are collected by get_document
        and the verbatim guard keys off ``verified_disclosures`` which is
        populated on the same path. Here we just clear the queue.
        """
        pending = self._ctx["pending_citations"]
        if pending:
            pending.clear()

    # -- WS + helpers ---------------------------------------------

    async def _send(self, data: dict[str, Any]) -> None:
        try:
            await self._ws.send_json(data)
        except Exception:  # noqa: BLE001
            logger.debug("DartEngine: WebSocket send failed")
            self._cancelled = True

    @staticmethod
    def _describe(name: str, inputs: dict[str, Any]) -> str:
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
