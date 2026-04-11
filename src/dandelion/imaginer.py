"""Theme agent — researches and imagines in a single CLI session."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

from pydantic import BaseModel

from src.config.settings import get_settings
from src.dandelion.schemas import Imagination
from src.overtime.runner import _get_rate_limit_wait
from src.utils.bridge_factory import get_bridge

logger = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


class _SingleImagination(BaseModel):
    title: str
    content: str
    time_months: int


class _ThemeImaginationsOutput(BaseModel):
    imaginations: list[_SingleImagination]


_THEME_SESSION_SYSTEM = """\
You are a foresight researcher and creative futurist.

TWO phases — be fast and focused:

## Phase 1: Quick Research (2-3 searches max)
Use WebSearch to find recent data (2025-2026) about the theme. Only WebFetch if a result looks especially valuable. Keep research brief — move to imagination quickly.

## Phase 2: Imagine {n} Scenarios
Based on your research, output {n} DIFFERENT future scenarios as JSON.

Rules:
- Each scenario: distinct outcome, mechanism, timeframe
- Bold and specific — concrete events, not vague trends
- Vary time_months: near-term (3-6mo), mid-term (12-24mo), long-term (36-60mo)
- Write in the same language as the user's context

Output ONLY this JSON (no markdown fences, no explanation before/after):
{{"imaginations": [{{"title": "concise title", "content": "2-3 paragraph description", "time_months": 12}}, ...]}}
"""


def _build_system(n: int) -> str:
    return _THEME_SESSION_SYSTEM.format(n=n)


def _build_user(theme_name: str, theme_description: str, common_context: str) -> str:
    return (
        f"## Theme: {theme_name}\n"
        f"{theme_description}\n\n"
        f"## Context\n{common_context}\n\n"
        f"Start with Phase 1 (research), then proceed to Phase 2 (imagination)."
    )


def _parse_imaginations(raw: str) -> list[dict] | None:
    """Extract JSON imaginations from raw LLM output."""
    # Try direct JSON parse
    text = raw.strip()
    for attempt in [text, _CODE_FENCE_RE.search(text)]:
        if attempt is None:
            continue
        candidate = attempt.group(1).strip() if hasattr(attempt, 'group') else attempt
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "imaginations" in data:
                return data["imaginations"]
        except (json.JSONDecodeError, AttributeError):
            continue

    # Last resort: find the last {...} block
    brace_depth = 0
    last_start = -1
    for i in range(len(text) - 1, -1, -1):
        if text[i] == '}':
            if brace_depth == 0:
                end = i + 1
            brace_depth += 1
        elif text[i] == '{':
            brace_depth -= 1
            if brace_depth == 0:
                last_start = i
                break
    if last_start >= 0:
        try:
            data = json.loads(text[last_start:end])
            if isinstance(data, dict) and "imaginations" in data:
                return data["imaginations"]
        except json.JSONDecodeError:
            pass

    return None


class Imaginer:
    """Runs one CLI session per theme: research → imagine N scenarios."""

    def __init__(self, **_kwargs):
        self._bridge = get_bridge()

    async def research_and_imagine(
        self,
        theme_id: str,
        theme_name: str,
        theme_description: str,
        common_context: str,
        on_event=None,
    ) -> list[Imagination]:
        """Single session: WebSearch research + structured imagination output."""
        settings = get_settings()
        n = settings.dandelion_seeds_per_theme

        system = _build_system(n)
        user_msg = _build_user(theme_name, theme_description, common_context)

        for attempt in range(2):
            try:
                raw = await self._bridge.raw_query(
                    system_prompt=system,
                    user_message=user_msg,
                    model=settings.dandelion_imaginer_model,
                    allowed_tools=["WebSearch", "WebFetch"],
                    timeout=settings.dandelion_session_timeout,
                    max_turns=settings.dandelion_max_turns,
                    on_event=on_event,
                )

                items = _parse_imaginations(raw)
                if not items:
                    logger.error("imaginer_parse_failed theme=%s raw_len=%d attempt=%d", theme_id, len(raw), attempt + 1)
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    return [self._fallback(theme_id)]

                results = []
                for item in items[:n]:
                    title = item.get("title", "제목 없음")
                    content = item.get("content", "")
                    time_months = item.get("time_months", 12)

                    sentences = content.split(". ")
                    summary = ". ".join(sentences[:2]) + "." if len(sentences) > 1 else content[:200]

                    results.append(Imagination(
                        id=uuid.uuid4().hex[:12],
                        theme_id=theme_id,
                        title=title,
                        summary=summary,
                        detail=content,
                        time_point=f"{time_months}개월 후",
                        time_months=time_months,
                    ))

                logger.info("imaginer_theme_ok theme=%s count=%d", theme_id, len(results))
                return results

            except Exception as exc:
                logger.error(
                    "imaginer_theme_failed theme=%s error=%s type=%s attempt=%d",
                    theme_id, exc, type(exc).__name__, attempt + 1,
                )
                # rate limit이면 리셋까지 대기 후 재시도
                wait_sec, is_rl = _get_rate_limit_wait()
                if is_rl:
                    logger.warning("imaginer_rate_limited theme=%s wait_s=%d", theme_id, wait_sec)
                    await asyncio.sleep(wait_sec)
                    continue
                if attempt == 0:
                    await asyncio.sleep(3)
                    continue
                return [self._fallback(theme_id)]

        return [self._fallback(theme_id)]

    def _fallback(self, theme_id: str) -> Imagination:
        return Imagination(
            id=uuid.uuid4().hex[:12],
            theme_id=theme_id,
            title="상상 생성 실패",
            summary="이 테마의 상상을 생성하지 못했습니다.",
            detail="시간 초과 또는 에러가 발생했습니다.",
            time_point="unknown",
            time_months=6,
        )
