"""DandelionEngine — orchestrates the multi-agent imagination pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
import re

import time

from src.config.settings import get_settings
from src.utils.bridge_factory import get_bridge
from src.utils.guards import safe_gather
from src.utils.notifier import notify_completion
from src.dandelion.schemas import (
    DandelionTree, Theme, ThemeAssignment, Seed, THEME_COLORS,
)
from src.dandelion.imaginer import Imaginer
from src.dandelion.prompts.ceo import CLARIFY_SYSTEM, THEME_DECISION_SYSTEM, build_ceo_user_message

logger = logging.getLogger(__name__)

CEO_MODEL = "sonnet"

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    m = _CODE_FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


class DandelionEngine:
    """Orchestrates: CEO (themes) → 4 theme sessions (research+imagine, 2 parallel) → Report."""

    def __init__(self, ws):
        self._ws = ws
        self._cancelled = False
        self._bridge = get_bridge()
        self._imaginer = Imaginer()

    def cancel(self):
        self._cancelled = True

    async def clarify(self, query: str, files: list[str]) -> list[str]:
        """Stage 0: Generate clarifying questions for the user."""
        user_msg = build_ceo_user_message(query, files)

        raw = await self._bridge.raw_query(
            system_prompt=CLARIFY_SYSTEM,
            user_message=user_msg,
            model=CEO_MODEL,
            allowed_tools=[],
            timeout=120,
        )

        text = _strip_code_fence(raw)
        try:
            data = json.loads(text)
            return data.get("questions", [])
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("clarify_failed: %s", exc)
            return []

    async def _progress(self, step: int, label: str, current: int = 0, total: int = 0):
        try:
            await self._ws.send_json({
                "type": "progress",
                "step": step,
                "label": label,
                "current": current,
                "total": total,
            })
        except Exception:
            pass

    async def run(self, query: str, files: list[str], clarify_answers: dict[str, str] | None = None) -> DandelionTree | None:
        """Run the full dandelion pipeline."""
        from datetime import datetime

        settings = get_settings()
        started = time.time()
        title = (query or "미래상상").strip().splitlines()[0][:80]

        # Stage 1: Theme decision (Sonnet, no tools)
        await self._progress(1, "테마 결정 중...")
        assignment = await self._decide_themes(query, files, clarify_answers)
        themes_dicts = [t.to_ws_dict() for t in assignment.themes]
        await self._ws.send_json({"type": "themes", "themes": themes_dicts})

        if self._cancelled:
            return None

        # Stage 2+3: Research & Imagination (2 themes at a time)
        n_per_theme = settings.dandelion_seeds_per_theme
        total_seeds = len(assignment.themes) * n_per_theme
        await self._progress(2, "리서치 & 상상 중...", 0, total_seeds)

        coros = [
            self._run_theme_session(theme, assignment.common_context, total_seeds)
            for theme in assignment.themes
        ]
        results = await safe_gather(
            coros,
            timeout_seconds=settings.dandelion_session_timeout,
            description="dandelion_themes",
            max_concurrency=settings.dandelion_max_concurrency,
        )

        # Collect seeds from results
        all_seeds: list[Seed] = []
        for theme, (success, result) in zip(assignment.themes, results):
            if success and isinstance(result, list):
                for img in result:
                    if img.title == "상상 생성 실패":
                        continue
                    seed = Seed(
                        id=img.id,
                        theme_id=img.theme_id,
                        title=img.title,
                        summary=img.summary,
                        detail=img.detail,
                        time_months=img.time_months,
                        weight=1,
                        source_count=1,
                    )
                    all_seeds.append(seed)
            else:
                error = result if not success else "unknown"
                logger.error("theme_failed theme=%s error=%s", theme.id, error)
                try:
                    await self._ws.send_json({
                        "type": "theme_error",
                        "theme_id": theme.id,
                        "message": str(error),
                    })
                except Exception:
                    pass

        # Send all seeds to frontend
        await self._progress(3, "상상 완료", total_seeds, total_seeds)
        for seed in all_seeds:
            try:
                await self._ws.send_json({
                    "type": "seed",
                    "theme_id": seed.theme_id,
                    "seed": seed.to_ws_dict(),
                })
            except Exception:
                pass

        tree = DandelionTree(
            query=query,
            themes=assignment.themes,
            seeds=all_seeds,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

        await self._ws.send_json({"type": "complete"})
        await notify_completion(
            kind="foresight",
            title=title,
            summary=f"테마 {len(assignment.themes)}개 · 상상 {len(all_seeds)}개 생성",
            duration_seconds=round(time.time() - started, 2),
            status="success",
        )
        return tree

    async def _run_theme_session(
        self, theme: Theme, common_context: str, total_seeds: int,
    ) -> list:
        """Run one theme session: research + imagine in a single CLI call."""
        if self._cancelled:
            return []

        async def _on_event(event: dict):
            """Stream tool_use events to frontend via WebSocket."""
            if event.get("action") != "tool_use":
                return
            tool = event.get("tool", "")
            tool_input = event.get("input", {})
            elapsed = event.get("elapsed", 0)

            if tool == "WebSearch":
                query = tool_input.get("query", tool_input.get("search_query", ""))
                label = f"🔍 {theme.name}: '{query}' 검색 중..."
            elif tool == "WebFetch":
                url = tool_input.get("url", "")
                # Truncate long URLs
                short_url = url[:60] + "..." if len(url) > 60 else url
                label = f"📄 {theme.name}: {short_url} 읽는 중..."
            else:
                label = f"⚙️ {theme.name}: {tool} 실행 중..."

            try:
                await self._ws.send_json({
                    "type": "session_log",
                    "theme_id": theme.id,
                    "label": label,
                    "elapsed": elapsed,
                })
            except Exception:
                pass

        result = await self._imaginer.research_and_imagine(
            theme_id=theme.id,
            theme_name=theme.name,
            theme_description=theme.description,
            common_context=common_context,
            on_event=_on_event,
        )

        done_count = len([img for img in result if img.title != "상상 생성 실패"])
        logger.info("theme_session_done theme=%s seeds=%d", theme.id, done_count)

        # Notify theme completion
        try:
            await self._ws.send_json({
                "type": "session_log",
                "theme_id": theme.id,
                "label": f"✅ {theme.name}: {done_count}개 상상 완료",
                "elapsed": 0,
            })
        except Exception:
            pass

        return result

    async def _decide_themes(self, query: str, files: list[str], clarify_answers: dict[str, str] | None = None) -> ThemeAssignment:
        """Stage 1: Sonnet decides 4 themes."""
        user_msg = build_ceo_user_message(query, files)
        if clarify_answers:
            user_msg += "\n\n--- 사용자 추가 답변 ---\n"
            for idx, answer in sorted(clarify_answers.items(), key=lambda x: int(x[0])):
                user_msg += f"Q{int(idx)+1}: {answer}\n"

        raw = await self._bridge.raw_query(
            system_prompt=THEME_DECISION_SYSTEM,
            user_message=user_msg,
            model=CEO_MODEL,
            allowed_tools=[],
            timeout=120,
        )

        text = _strip_code_fence(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"테마 결정 응답 파싱 실패: {exc}\nResponse: {text[:500]}") from exc

        themes = []
        for i, t in enumerate(data["themes"][:4]):
            themes.append(Theme(
                id=f"theme_{i}",
                name=t["name"],
                color=THEME_COLORS[i],
                description=t["description"],
            ))

        for j in range(len(themes), 4):
            themes.append(Theme(
                id=f"theme_{j}",
                name=f"추가 관점 {j+1}",
                color=THEME_COLORS[j],
                description="자동 생성된 보조 테마",
            ))

        return ThemeAssignment(
            themes=themes,
            common_context=data.get("common_context", query),
            user_query=query,
        )
