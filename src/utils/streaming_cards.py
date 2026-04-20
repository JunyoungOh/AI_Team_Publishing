"""Streaming card emission helpers — shared by all stream-json subprocess callers.

호출자 6곳(single_session, overtime/runner, secretary/chat_engine,
skill_builder/execution_streamer, foresight/engine, claude_code._run_subprocess_streaming)이
공유하는 stream-json block 파싱 + 카드 이벤트 emit 로직.

설계 요지:
- 두 종류 emit 경로를 CardEmitter로 추상화:
    from_session_id(sid)  → emit_mode_event 큐 사용 (5개 호출자)
    from_callback(fn)     → on_event 콜백 직접 호출 (Dandelion)
- assistant 메시지 블록: text(narration) + tool_use
- user 메시지 블록: tool_result(tool_done)
- text 블록은 1.5초 rate limit — 짧은 텍스트가 자주 와도 카드가 과하게 깜빡이지 않음
- tool_result는 tool_use_id → tool_name 매핑(호출자 보유)으로 도구별 라벨 적용
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from src.utils.logging import get_logger

_logger = get_logger(agent_id="streaming_cards")

# ── Constants ─────────────────────────────────────────────

# UI에 노출하지 않을 내부 도구 (card-event-handler.js의 _HIDDEN_TOOLS와 동기화)
_HIDDEN_TOOLS: frozenset[str] = frozenset({
    "ToolSearch", "TodoRead", "TodoWrite",
})

# 도구별 메타데이터 — use_label(진행중), done_label(완료), detail_field(Agent 등에서 발췌)
_TOOL_META: dict[str, dict[str, str]] = {
    "WebSearch": {
        "use_label": "🔍 웹 검색 중...",
        "done_label": "✅ 검색 결과 받음",
    },
    "WebFetch": {
        "use_label": "🌐 웹 페이지 수집 중...",
        "done_label": "✅ 페이지 수집 완료",
    },
    "Agent": {
        "use_label": "🤖 서브에이전트 실행 중...",
        "done_label": "✅ 서브에이전트 완료",
    },
    "Write": {
        "use_label": "📝 파일 작성 중...",
        "done_label": "✅ 파일 저장됨",
    },
    "Read": {
        "use_label": "📄 파일 읽는 중...",
        "done_label": "✅ 파일 읽음",
    },
    "Bash": {
        "use_label": "⚙️ 명령 실행 중...",
        "done_label": "✅ 명령 실행 완료",
    },
    "Glob": {
        "use_label": "📂 파일 검색 중...",
        "done_label": "✅ 파일 검색 완료",
    },
    "Grep": {
        "use_label": "🔎 코드 검색 중...",
        "done_label": "✅ 코드 검색 완료",
    },
    "mcp__firecrawl__firecrawl_scrape": {
        "use_label": "🕷️ 웹 스크래핑 중...",
        "done_label": "✅ 스크래핑 완료",
    },
}

_NARRATION_RATE_LIMIT_SEC: float = 1.5
_NARRATION_PREVIEW_LEN: int = 60
_AGENT_DETAIL_LEN: int = 80

# 하트비트(침묵 감지)
_HEARTBEAT_SILENT_THRESHOLD_SEC: float = 15.0  # 이 시간 이상 침묵이면 하트비트 표시
_HEARTBEAT_CHECK_INTERVAL_SEC: float = 5.0     # 이 주기로 침묵 여부 체크

# narration 발췌 시 공백으로 치환할 장식 문자
# ★, 각종 대시/수평선, 마크다운 헤딩/리스트/코드 프레임, 블록인용 등
# `-`는 한글 preview에서 손해 거의 없고 list item 프레임을 잘 지움
_DECORATION_RE = re.compile(r"[★⭐▼▶◆■□●○•·\-─—━`*#=|>_~]+")
_MEANINGFUL_MIN_WORD_CHARS = 10  # 영숫자+한글 합이 이 미만이면 장식만 있는 것으로 보고 스킵


def _count_word_chars(text: str) -> int:
    """영숫자 + 한글 음절 개수. 장식·공백·문장부호 제외."""
    count = 0
    for c in text:
        if c.isalnum() or "\uAC00" <= c <= "\uD7A3":
            count += 1
    return count


def _tool_use_label(tool_name: str) -> str:
    """도구 사용 시작 라벨. 미지 도구는 🔧 prefix."""
    meta = _TOOL_META.get(tool_name)
    if meta:
        return meta["use_label"]
    return f"🔧 {tool_name}"


def _tool_done_label(tool_name: str, content_length: int | None = None) -> str:
    """도구 완료 라벨. 정량(content 길이)이 있으면 합쳐서 표시."""
    meta = _TOOL_META.get(tool_name)
    base = meta["done_label"] if meta else f"✅ {tool_name} 결과 받음"
    if content_length is not None and content_length > 0:
        return f"{base} ({content_length:,}자)"
    return base


def _truncate_narration(text: str) -> str:
    """긴 텍스트를 narration 카드용으로 발췌.

    파이프라인:
    1) 장식 문자(★, ─, —, `, *, #, -, 등)를 모두 공백으로 치환
       → "★ Insight ─────", 백틱 프레임, list item 대시 등이 한꺼번에 제거됨
    2) 연속 공백·줄바꿈을 단일 공백으로 정규화
    3) 영숫자+한글 글자 수가 _MEANINGFUL_MIN_WORD_CHARS 미만이면 빈 문자열 반환
       → 호출자가 emit 스킵. 다음 text 블록이 도착하면 누적 버퍼에 장식이
       섞여 있어도 이 함수가 장식만 걷어내고 그 다음 의미 내용을 보여줌
    4) 60자 초과 시 "..." 말줄임
    """
    cleaned = _DECORATION_RE.sub(" ", text)
    cleaned = " ".join(cleaned.split())
    if _count_word_chars(cleaned) < _MEANINGFUL_MIN_WORD_CHARS:
        return ""
    if len(cleaned) > _NARRATION_PREVIEW_LEN:
        return cleaned[:_NARRATION_PREVIEW_LEN].rstrip() + "..."
    return cleaned


# ── CardEmitter ──────────────────────────────────────────

_EventCallback = Callable[[dict], Any]  # dict → Any (sync 또는 coroutine)


@dataclass
class CardEmitter:
    """카드 이벤트 emit 추상화. 호출자별 두 가지 인스턴스화 경로.

    내부 상태:
    - _last_narration_ts: narration rate-limit용
    - _narration_buffer: narration 누적 버퍼
    - _last_event_ts: 하트비트 침묵 감지용 (heartbeat 제외한 모든 emit마다 갱신)

    한 세션당 인스턴스 하나를 유지해야 함 (stream loop 전체에서 재사용).
    """
    session_id: str | None = None
    callback: _EventCallback | None = None
    _last_narration_ts: float = field(default=0.0, init=False)
    _narration_buffer: list[str] = field(default_factory=list, init=False)
    _last_event_ts: float = field(default_factory=time.monotonic, init=False)

    @classmethod
    def from_session_id(cls, session_id: str) -> "CardEmitter":
        """emit_mode_event 큐 기반 (5개 호출자용)."""
        return cls(session_id=session_id)

    @classmethod
    def from_callback(cls, callback: _EventCallback) -> "CardEmitter":
        """on_event 콜백 직접 호출 (Dandelion용)."""
        return cls(callback=callback)

    async def emit(self, event: dict) -> None:
        """이벤트 dispatch. 두 경로 모두 지원.

        heartbeat 이벤트는 silence 타이머를 리셋하지 않음 — 그래야
        침묵이 지속되는 동안 하트비트가 계속 발화하며 elapsed 카운터가 증가.
        """
        action = (event.get("data") or {}).get("action", "")
        is_heartbeat = action == "heartbeat"
        if not is_heartbeat:
            self._last_event_ts = time.monotonic()

        if self.callback is not None:
            result = self.callback(event)
            if asyncio.iscoroutine(result):
                await result
            return

        if self.session_id is not None:
            # Lazy import — modes/common.py는 카드 경로만 쓰는 호출자에겐 선택적
            from src.modes.common import emit_mode_event
            emit_mode_event(self.session_id, event)


# ── Block handlers ───────────────────────────────────────

async def handle_assistant_block(
    block: dict,
    *,
    emitter: CardEmitter,
    elapsed: float,
    text_accumulator: list[str],
    tool_count_ref: list[int],
    tool_use_map: dict[str, str],
) -> None:
    """assistant 메시지의 content 블록 1개 처리.

    Args:
        block: stream-json assistant message의 content 블록 1개
        emitter: CardEmitter 인스턴스 (한 세션에 하나, 재사용)
        elapsed: 세션 시작 후 경과 초
        text_accumulator: 호출자가 보유한 full_text 버퍼 (항상 누적)
        tool_count_ref: 호출자가 보유한 [카운터] — 1-element mutable list
        tool_use_map: tool_use_id → tool_name 매핑 (호출자가 보유, 여기서 갱신)

    처리되는 블록 타입:
        text      → text_accumulator에 누적 + rate limit 통과 시 narration emit
        tool_use  → tool_use_map 갱신, tool_count 증가, tool_use 이벤트 emit
        그 외     → 무시 (thinking 등)
    """
    block_type = block.get("type")

    if block_type == "text":
        text = block.get("text", "")
        if not text:
            return
        text_accumulator.append(text)
        emitter._narration_buffer.append(text)

        now = time.monotonic()
        if now - emitter._last_narration_ts < _NARRATION_RATE_LIMIT_SEC:
            return  # rate limit — 누적만 하고 emit 스킵

        combined = "".join(emitter._narration_buffer)
        preview = _truncate_narration(combined)
        if not preview:
            return

        await emitter.emit({
            "type": "activity",
            "data": {
                "action": "narration",
                "message": f"💭 {preview}",
                "elapsed": elapsed,
            },
        })
        emitter._last_narration_ts = now
        emitter._narration_buffer.clear()
        return

    if block_type == "tool_use":
        tool_name = block.get("name", "")
        tool_id = block.get("id", "")
        if tool_id:
            tool_use_map[tool_id] = tool_name

        if tool_name in _HIDDEN_TOOLS:
            return

        tool_count_ref[0] += 1

        # Agent 도구는 description/prompt 발췌
        detail = ""
        if tool_name == "Agent":
            inp = block.get("input", {}) or {}
            raw = inp.get("description") or inp.get("prompt") or ""
            detail = str(raw)[:_AGENT_DETAIL_LEN]

        await emitter.emit({
            "type": "activity",
            "data": {
                "action": "tool_use",
                "tool": tool_name,
                "message": _tool_use_label(tool_name),
                "detail": detail,
                "elapsed": elapsed,
                "tool_count": tool_count_ref[0],
            },
        })
        return

    # 그 외 블록 (thinking 등) 무시


async def handle_user_block(
    block: dict,
    *,
    emitter: CardEmitter,
    elapsed: float,
    tool_use_map: dict[str, str],
) -> None:
    """user 메시지의 content 블록 1개 처리 (tool_result 전용).

    Args:
        block: stream-json user message의 content 블록 1개
        emitter: CardEmitter 인스턴스
        elapsed: 세션 시작 후 경과 초
        tool_use_map: 이전 tool_use 블록들이 채워둔 id→name 매핑

    처리되는 블록 타입:
        tool_result → is_error면 ⚠️, 정상이면 정량 합친 tool_done emit
        그 외       → 무시
    """
    if block.get("type") != "tool_result":
        return

    tool_use_id = block.get("tool_use_id", "")
    tool_name = tool_use_map.get(tool_use_id, "")

    if tool_name in _HIDDEN_TOOLS:
        return

    is_error = bool(block.get("is_error", False))

    # content 길이 추출 — content는 list[dict] 또는 str 형태 둘 다 가능
    content_length: int | None = None
    content = block.get("content")
    if isinstance(content, str):
        content_length = len(content)
    elif isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                total += len(item.get("text", ""))
            elif isinstance(item, str):
                total += len(item)
        if total > 0:
            content_length = total

    if is_error:
        # content에서 실패 원인 프리뷰를 뽑아 서버 로그에 기록한다.
        # UI 카드는 친절 라벨만 유지 (원인 노출은 진단자 몫).
        # content는 str 또는 list[dict|str] 형태 둘 다 가능하며, 위의 content_length
        # 계산 블록은 is_error=False 경로에서만 돌기 때문에 여기서 별도로 파싱한다.
        preview = ""
        if isinstance(content, str):
            preview = content
        elif isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            preview = "".join(parts)
        preview = " ".join(preview.split())[:300]  # 개행·중복공백 정규화 + 상한

        _logger.warning(
            "tool_failed",
            tool=tool_name or "unknown",
            tool_use_id=tool_use_id,
            preview=preview,
        )

        label = f"⚠️ {tool_name or '도구'} 실패" if tool_name else "⚠️ 도구 실패"
        await emitter.emit({
            "type": "activity",
            "data": {
                "action": "tool_done",
                "tool": tool_name,
                "message": label,
                "is_error": True,
                "elapsed": elapsed,
            },
        })
        return

    label = _tool_done_label(tool_name or "도구", content_length=content_length)
    await emitter.emit({
        "type": "activity",
        "data": {
            "action": "tool_done",
            "tool": tool_name,
            "message": label,
            "content_length": content_length or 0,
            "elapsed": elapsed,
        },
    })


# ── Heartbeat loop ───────────────────────────────────────

async def heartbeat_loop(
    emitter: CardEmitter,
    *,
    start_time: float,
    silent_threshold: float = _HEARTBEAT_SILENT_THRESHOLD_SEC,
    check_interval: float = _HEARTBEAT_CHECK_INTERVAL_SEC,
) -> None:
    """stream이 silent_threshold 이상 조용하면 heartbeat 카드를 주기적으로 emit.

    왜 필요한가:
    - 모델이 Write 같이 큰 content를 inline으로 담아 도구를 호출할 때,
      그 한 줄이 stream-json으로 완성되기까지 수십 초 동안 subprocess stdout이
      block됨. 이 동안 일반 이벤트는 발화하지 못함.
    - 또한 extended thinking 중이거나 긴 텍스트 생성 중엔 도구 호출이 없어
      카드 화면이 얼어붙음.
    - 하트비트는 "AI가 작업 중"이라는 생존 신호를 주고, UI 측에서 이 이벤트는
      마지막 피드 항목을 in-place로 교체하여 화면 오염 없이 elapsed만 증가.

    호출 규약:
    - 호출자는 stream loop 시작 전에 `asyncio.create_task(heartbeat_loop(...))`
    - finally 블록에서 task.cancel() + await (CancelledError 흡수)

    heartbeat emit은 `CardEmitter.emit`이 `_last_event_ts`를 갱신하지 않도록
    예외 처리하므로, 침묵이 지속되는 동안 계속 발화됨 (elapsed 카운터 증가).
    """
    try:
        while True:
            await asyncio.sleep(check_interval)
            silent_for = time.monotonic() - emitter._last_event_ts
            if silent_for < silent_threshold:
                continue
            elapsed_total = time.time() - start_time
            await emitter.emit({
                "type": "activity",
                "data": {
                    "action": "heartbeat",
                    "message": f"⏳ AI가 작업 중... ({int(elapsed_total)}s)",
                    "elapsed": round(elapsed_total, 1),
                },
            })
    except asyncio.CancelledError:
        return
