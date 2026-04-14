"""LawEngine — CLI-bridge tool loop for the AI Law mode.

Mirrors the shape of ``src/foresight/engine.py`` but trimmed to the bare
essentials: one system prompt + six custom tools + a verbatim guard that
rejects article references the LLM fabricates.
"""
from __future__ import annotations

import json
import logging
import re
from functools import partial
from typing import Any

from src.config.settings import get_settings
from src.law.prompts.system import build_system_prompt
from src.law.tools import LAW_TOOL_EXECUTORS, LAW_TOOL_SCHEMAS, make_session_context
from src.utils.bridge_factory import get_bridge

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "⚠️ 본 답변은 법령 원문을 기반으로 한 일반 정보 제공이며, 법률 자문이 아닙니다. "
    "구체적 사안은 반드시 변호사와 상담하시기 바랍니다."
)

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_ARTICLE_REF_RE = re.compile(r"제\s*(\d+)\s*조(?:의\s*(\d+))?")

# Keyword-mode questions should terminate in 2–3 turns. Situation-mode may need
# a few more for cross-issue searches. Capping at 5 keeps pathological loops
# from leaving the user staring at "turn 8/10" for a minute.
_MAX_TURNS = 5

# Keys that give away a raw law.go.kr JSON blob when the LLM echoes tool
# output into its response text. Used by _strip_json_echo to scrub the
# display text before streaming it to the frontend.
_JSON_ECHO_KEYS = (
    '"법령',
    '"조문',
    '"MST"',
    '"source_url"',
    '"mst"',
    '"article"',
    '"jo_code"',
)

# LLM sometimes constructs URLs like
#   https://www.law.go.kr/법령/개인정보보호법/(20250918,497716,20250916)/제15조
# which markdown parsers truncate at the unbalanced ")" inside a link.
# This regex captures the "(...,...)" revision block so we can drop it.
_URL_PAREN_BLOCK_RE = re.compile(r"/\([^)]*\)")

# Stray Claude Code tool_use markers that leak through when the CLI fell
# back to returning raw stream-json (text blocks were empty).
_TOOLU_MARKER_RE = re.compile(r"toolu_[A-Za-z0-9]+")

# Orphan tool-result section labels that the LLM sometimes writes as a
# narration header above the JSON it's about to paste. After
# ``_strip_json_echo`` removes the JSON, the label is left behind.
# Matches things like "[Law Search Result]", "[Tool Result: law_search]",
# "[law_get_article result]" at the start of a line.
_TOOL_RESULT_LABEL_RE = re.compile(
    r"^\s*\[\s*(?:law[_ ]?(?:search|article|get|precedent|prec|expc)|"
    r"tool[_ ]?result(?:[:\s]+\w+)?|"
    r"law\s+(?:search|article|precedent|decision|interpretation)\s+result)"
    r"[\s\w]*\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _build_tool_instructions(schemas: dict[str, dict[str, Any]]) -> str:
    lines = [
        "You have access to the following custom tools. To use a tool, respond with a "
        "<tool_call> block containing JSON with 'name' and 'input' keys.\n"
        "Example: <tool_call>{\"name\": \"law_search\", \"input\": {\"query\": \"개인정보보호법\"}}</tool_call>\n"
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
            logger.debug("LawEngine: failed to parse tool_call JSON: %s", match.group(1)[:200])
    return calls


def _strip_tool_calls(text: str) -> str:
    return _TOOL_CALL_RE.sub("", text).strip()


def _find_balanced_end(text: str, start: int) -> int:
    """Return the index of the matching ``]``/``}`` for the bracket at *start*.

    Walks the text with a depth counter while respecting JSON string literals
    (so brackets inside ``"..."`` don't throw off the balance). Returns ``-1``
    if no balanced closer is found before end-of-string.
    """
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
    """Remove JSON-shaped blobs that the LLM copied from tool results.

    Sonnet often "shows its work" by dumping a tool result (or a Korean-key
    paraphrase of it) straight into its response text. We scan for ``[`` or
    ``{``, find the balanced closer, and if the enclosed block contains any
    of our law.go.kr marker keys, we drop the block entirely. Collapses
    leftover blank runs at the end.
    """
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
                    # Drop the block. Also swallow a single trailing newline so
                    # we don't leave a stray blank line behind.
                    i = end + 1
                    if i < n and text[i] == "\n":
                        i += 1
                    continue
        out.append(ch)
        i += 1
    # Collapse 3+ consecutive newlines that result from the strip.
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _sanitise_urls(text: str) -> str:
    """Remove the parenthetical revision block from malformed law.go.kr URLs."""
    return _URL_PAREN_BLOCK_RE.sub("", text)


def _strip_toolu_markers(text: str) -> str:
    """Remove stray ``toolu_XXX`` markers that leak from raw stream-json."""
    if not text or "toolu_" not in text:
        return text
    cleaned = _TOOLU_MARKER_RE.sub("", text)
    # Collapse empty lines left behind.
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _strip_tool_result_labels(text: str) -> str:
    """Remove orphan ``[Law Search Result]`` style headers the LLM narrates."""
    if not text:
        return text
    cleaned = _TOOL_RESULT_LABEL_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _parse_cli_response(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse the CLI's raw output into (clean_text, native_tool_calls).

    Handles two shapes:

    1. **Plain text** — what we get in the happy path, when
       ``_extract_all_assistant_text`` successfully collected ``text`` blocks.
       In that case we just return ``(raw, [])``.

    2. **Raw stream-json NDJSON** — what we get when the CLI fell back
       because the assistant turn only contained ``tool_use`` blocks (no
       text). We walk each NDJSON line, collect text blocks, and promote
       every ``tool_use`` block into a call dict that our engine can
       execute via ``_execute_tool``.

    Returns ``(text, native_tool_calls)`` where ``text`` is whatever plain
    text the CLI gave us (possibly empty) and ``native_tool_calls`` is a
    list of ``{"name": str, "input": dict}`` entries that came in through
    the native protocol rather than our ``<tool_call>`` wrapper.
    """
    if not raw:
        return "", []
    stripped = raw.lstrip()
    # Heuristic: NDJSON starts with "{" on its first non-blank line and
    # contains at least one of the stream-json event markers.
    if not stripped.startswith("{") or '"type"' not in raw:
        return raw, []
    # Walk every NDJSON line we can parse.
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


class LawEngine:
    """One chat session against the law-assistant prompt."""

    def __init__(self, ws: Any) -> None:
        self._settings = get_settings()
        self._bridge = get_bridge()
        self._ws = ws
        self._system_prompt = build_system_prompt()
        self._tool_instructions = _build_tool_instructions(LAW_TOOL_SCHEMAS)
        self._messages: list[dict[str, Any]] = []
        self._cancelled = False
        self._mode = "flash"  # "flash" | "think"

        ctx = make_session_context()
        self._ctx = ctx
        self._tool_executors = {
            name: partial(fn, ctx) for name, fn in LAW_TOOL_EXECUTORS.items()
        }
        # Preserve the longest substantive answer across turns so a max-turns
        # bail can still flush something useful instead of dropping the work.
        self._best_display_text: str = ""

    # -- Public API -----------------------------------------------

    def cancel(self) -> None:
        self._cancelled = True

    def set_mode(self, mode: str) -> None:
        if mode in ("flash", "think"):
            self._mode = mode

    async def send_message(self, content: str) -> None:
        self._cancelled = False
        self._best_display_text = ""
        self._messages.append({"role": "user", "content": content})
        await self._run_loop()

    # -- Internals ------------------------------------------------

    def _effort(self) -> str:
        return "high" if self._mode == "think" else "low"

    async def _run_loop(self) -> None:
        full_system = f"{self._system_prompt}\n\n{self._tool_instructions}"
        turns = 0

        while turns < _MAX_TURNS and not self._cancelled:
            turns += 1
            user_message = self._build_conversation_text()

            await self._send({
                "type": "law_tool_status",
                "data": {"tool": "", "status": f"분석 중... (턴 {turns}/{_MAX_TURNS})"},
            })

            try:
                response_text = await self._bridge.raw_query(
                    system_prompt=full_system,
                    user_message=user_message,
                    model="sonnet",
                    # WebSearch/WebFetch are "decoy" native tools: we never
                    # want the LLM to actually use them for legal queries
                    # (the prompt forbids it), but having them present
                    # prevents the Sonnet 4.6 confusion where an empty
                    # allowed_tools list pushes the model toward native
                    # tool_use for our custom tools.
                    allowed_tools=["WebSearch", "WebFetch"],
                    max_turns=5,
                    timeout=240,
                    effort=self._effort(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("LawEngine bridge error")
                # If we've already accumulated a substantive answer on
                # earlier turns, flush that instead of dropping the work.
                if self._best_display_text:
                    final_text = self._finalise(self._best_display_text)
                    tail = final_text[len(self._best_display_text):]
                    if tail:
                        await self._send({
                            "type": "law_stream",
                            "data": {"token": tail, "done": False},
                        })
                    await self._flush_pending_citations()
                    await self._send({
                        "type": "law_stream",
                        "data": {
                            "token": f"\n\n— *(일부 분석 단계에서 오류가 발생했지만, 확보된 답변을 반환합니다: {type(exc).__name__})*",
                            "done": True,
                        },
                    })
                else:
                    await self._send({
                        "type": "law_error",
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

            # Detect CLI fallback (raw stream-json NDJSON) and extract both
            # plain text and any native tool_use blocks that leaked through.
            cli_text, native_tool_calls = _parse_cli_response(response_text)
            if native_tool_calls:
                logger.info(
                    "LawEngine: promoted %d native tool_use call(s) from stream-json",
                    len(native_tool_calls),
                )
                # Store the assistant turn as the clean text (not the NDJSON
                # dump) so subsequent turns don't re-feed raw NDJSON back.
                response_text = cli_text
            display_text = _strip_tool_calls(response_text)
            display_text = _strip_json_echo(display_text)
            display_text = _strip_tool_result_labels(display_text)
            display_text = _strip_toolu_markers(display_text)
            display_text = _sanitise_urls(display_text)
            if display_text:
                await self._send({
                    "type": "law_stream",
                    "data": {"token": display_text, "done": False},
                })
                if len(display_text) > len(self._best_display_text):
                    self._best_display_text = display_text

            self._messages.append({"role": "assistant", "content": response_text})

            xml_tool_calls = _parse_tool_calls(response_text)
            tool_calls = xml_tool_calls + native_tool_calls
            # Early termination: if the LLM already wrote a substantive answer
            # (disclaimer present), treat it as final — even if it also
            # appended more <tool_call> blocks to "double-check" itself.
            if tool_calls and self._looks_final(display_text):
                logger.info(
                    "LawEngine: early termination — answer len=%d, %d tool_calls ignored",
                    len(display_text), len(tool_calls),
                )
                tool_calls = []

            if not tool_calls:
                # Finalise: run verbatim guard, append disclaimer, flush citations.
                final_text = self._finalise(display_text)
                if final_text != display_text:
                    await self._send({
                        "type": "law_stream",
                        "data": {"token": f"\n\n{final_text[len(display_text):]}", "done": False},
                    })
                await self._flush_pending_citations()
                await self._send({
                    "type": "law_stream",
                    "data": {"token": "", "done": True},
                })
                return

            # Execute tools and feed results back.
            # Wrap each result in a non-markdown XML-ish tag so the LLM has
            # no "header + body" template to mimic in its own response text.
            # The ``_strip_tool_result_labels`` defence catches any labels
            # that leak through anyway.
            tool_results: list[str] = []
            for call in tool_calls:
                name = call.get("name", "")
                inputs = call.get("input", {}) or {}
                await self._send({
                    "type": "law_tool_status",
                    "data": {"tool": name, "status": self._describe(name, inputs)},
                })
                result = await self._execute_tool(name, inputs)
                tool_results.append(
                    f"<observation tool=\"{name}\">\n{result[:8000]}\n</observation>"
                )

            # After each tool round, stream any new citation cards to the UI
            # so the user sees the original text building up alongside the answer.
            await self._flush_pending_citations()

            self._messages.append({
                "role": "user",
                "content": (
                    "<tool-observations>\n"
                    + "\n".join(tool_results)
                    + "\n</tool-observations>\n\n"
                    "위 관찰 결과를 바탕으로 사용자에게 자연어로 답변을 이어가십시오. "
                    "확보되지 않은 조문은 언급하지 마시고, "
                    "관찰 결과의 원문(JSON, XML, 라벨 등)은 절대 응답 본문에 복사하지 마십시오."
                ),
            })
            self._prune_history()

        if not self._cancelled:
            # Max turns hit. If we saw any substantive answer earlier, treat
            # the longest one as final and finalise it (verbatim guard +
            # disclaimer). Otherwise fall back to the generic bail message.
            if self._best_display_text:
                final_text = self._finalise(self._best_display_text)
                tail = final_text[len(self._best_display_text):]
                if tail:
                    await self._send({
                        "type": "law_stream",
                        "data": {"token": tail, "done": False},
                    })
                await self._flush_pending_citations()
                await self._send({
                    "type": "law_stream",
                    "data": {
                        "token": "\n\n— *(최대 분석 횟수 도달 — 가장 완성된 답변을 반환합니다)*",
                        "done": True,
                    },
                })
            else:
                await self._send({
                    "type": "law_stream",
                    "data": {
                        "token": "\n\n[AI 법령] 최대 실행 횟수에 도달했습니다. 질문을 좀 더 구체적으로 다시 해주세요.",
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
            logger.exception("Law tool execution error (%s)", name)
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
            and m["content"].startswith("Tool execution results:")
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
        """Detect an already-complete answer so we can ignore trailing tool calls.

        Only the disclaimer footer counts as a reliable "I'm done" signal.
        Length-based heuristics are too fragile — they used to fire before
        ``law_get_article`` had even been called, which suppressed citation
        cards. The disclaimer is an unambiguous marker that the LLM wrote
        a full answer.
        """
        if not text:
            return False
        return _DISCLAIMER[:20] in text

    def _finalise(self, text: str) -> str:
        """Run verbatim guard + ensure disclaimer is present + clean URLs."""
        guarded = self._verbatim_guard(text)
        guarded = _sanitise_urls(guarded)
        if _DISCLAIMER[:20] not in guarded:
            guarded = guarded.rstrip() + f"\n\n> {_DISCLAIMER}"
        return guarded

    def _verbatim_guard(self, text: str) -> str:
        """Redact article references that were never backed by a tool call.

        The LLM is allowed to mention an article only if we have a
        verified (mst, jo_code) pair for it in the session context.
        Unverified matches are replaced with an inline warning so the
        user can see *something was blocked* rather than silently losing text.
        """
        verified_codes = {code for _mst, code in self._ctx["verified_articles"]}
        if not verified_codes:
            # No articles were fetched — the LLM shouldn't be citing specific
            # articles at all. Warn instead of rewriting the whole answer.
            if _ARTICLE_REF_RE.search(text):
                return text + (
                    "\n\n> ⚠️ 조문 원문을 조회하지 못했으므로 위 답변의 조문 인용은 "
                    "참고용이며, 직접 law.go.kr에서 확인해 주십시오."
                )
            return text

        def _replace(match: re.Match[str]) -> str:
            main = int(match.group(1))
            branch = int(match.group(2)) if match.group(2) else 0
            code = f"{main:04d}{branch:02d}"
            if code in verified_codes:
                return match.group(0)
            return f"{match.group(0)} [⚠️ 원문 미확보 — 확인 필요]"

        return _ARTICLE_REF_RE.sub(_replace, text)

    async def _flush_pending_citations(self) -> None:
        pending = self._ctx["pending_citations"]
        if not pending:
            return
        for card in pending:
            await self._send({"type": "law_citation", "data": card})
        pending.clear()

    # -- WS + helpers ---------------------------------------------

    async def _send(self, data: dict[str, Any]) -> None:
        try:
            await self._ws.send_json(data)
        except Exception:  # noqa: BLE001
            logger.debug("LawEngine: WebSocket send failed")
            self._cancelled = True

    @staticmethod
    def _describe(name: str, inputs: dict[str, Any]) -> str:
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
