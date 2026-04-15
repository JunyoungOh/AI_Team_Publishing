"""Opening nodes — split into prep (moderator) + speak (participants).

Two-node design so LangGraph streams a phase event between them,
giving the frontend visual feedback during the long opening sequence.

  opening_prep  → moderator generates per-participant prompts  (~10-15s)
  opening_speak → all participants respond in parallel          (~20-45s)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from pydantic import BaseModel

from src.config.settings import get_settings
from src.discussion.prompts.moderator import (
    MODERATOR_HUMAN_SECTION,
    MODERATOR_OPENING,
    STYLE_DESCRIPTIONS,
)
from src.discussion.prompts.participant import PARTICIPANT_SYSTEM
from src.discussion.state import DiscussionState, Utterance, HUMAN_SPEAKER_ID
from src.utils.bridge_factory import get_bridge

logger = logging.getLogger(__name__)


class OpeningPrompts(BaseModel):
    opening_prompts: list[dict]


def _format_participants(config) -> str:
    lines = [
        f"- {p.name} ({p.id}): {p.persona}"
        for p in config.participants
    ]
    if config.human_participant:
        hp = config.human_participant
        desc = hp.persona if hp.persona else "실제 사용자 (AI가 아님)"
        lines.append(f"- {hp.name} ({HUMAN_SPEAKER_ID}): {desc}")
    return "\n".join(lines)


# ── Node 1: Moderator prepares opening prompts ──────────────


async def discussion_opening_prep(state: DiscussionState) -> dict:
    """Moderator generates per-participant opening prompts."""
    config = state["config"]
    bridge = get_bridge()
    participants_info = _format_participants(config)
    style_desc = STYLE_DESCRIPTIONS.get(config.style, STYLE_DESCRIPTIONS["free"])

    mod_prompt = MODERATOR_OPENING.format(
        topic=config.topic,
        participants_info=participants_info,
        style_desc=style_desc,
    )
    if config.human_participant:
        mod_prompt += MODERATOR_HUMAN_SECTION.format(
            human_name=config.human_participant.name,
        )

    try:
        mod_result = await bridge.structured_query(
            system_prompt=mod_prompt,
            user_message=f"토론 주제: {config.topic}",
            output_schema=OpeningPrompts,
            model=config.model_moderator,
            allowed_tools=[],
            timeout=120,
            max_turns=3,
            effort="medium",
        )
        opening_map = {
            op["speaker_id"]: op["instruction"]
            for op in mod_result.opening_prompts
        }
    except Exception as e:
        logger.warning("opening_prep_llm_failed: %s", e)
        opening_map = {
            p.id: f"{config.topic}에 대한 입장을 밝혀주세요."
            for p in config.participants
        }
    finally:
        await bridge.close()

    return {
        "phase": "opening_speak",
        "moderator_instruction": json.dumps(opening_map, ensure_ascii=False),
    }


# ── Node 2: Participants speak in parallel ───────────────────


async def discussion_opening_speak(state: DiscussionState) -> dict:
    """All participants give opening statements in parallel.

    Each AI participant's first call creates a persistent Claude CLI session
    via ``--session-id`` (UUID generated in setup). Subsequent rounds reuse
    that session via ``--resume`` (see speak.py), so the
    persona + topic system prompt is sent only once.

    Cold-start cost: starting N CLI subprocesses simultaneously can race and
    cause subprocess failures (the bug we hit before). A small semaphore
    caps concurrent startups to 2, which empirically avoids the race while
    still keeping opening fast.
    """
    config = state["config"]
    bridge = get_bridge()
    participant_sessions = state.get("participant_sessions") or {}

    # Recover opening_map from state (stored as JSON by prep node)
    raw = state.get("moderator_instruction", "")
    try:
        opening_map = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        opening_map = {}

    cold_start_sem = asyncio.Semaphore(2)

    async def _speak(participant):
        instruction = opening_map.get(participant.id, f"{config.topic}에 대한 입장을 밝혀주세요.")
        prompt = PARTICIPANT_SYSTEM.format(
            name=participant.name,
            persona=participant.persona,
            topic=config.topic,
            conversation_so_far="(토론 시작 — 첫 발언입니다)",
            instruction=instruction,
        )
        sid = participant_sessions.get(participant.id)
        async with cold_start_sem:
            try:
                text = await bridge.raw_query(
                    system_prompt=prompt,
                    user_message=instruction,
                    model=config.model_participant,
                    # WebSearch + WebFetch are Claude Code built-ins (no MCP
                    # cold start). Speakers self-decide when to fact-check
                    # instead of routing through a moderator gatekeeper.
                    allowed_tools=["WebSearch", "WebFetch"],
                    timeout=120,
                    max_turns=2,  # 1 tool call + final answer if needed
                    effort="medium",
                    session_id=sid,
                )
            except Exception as e:
                logger.warning("opening_speak_failed: %s (speaker=%s)", e, participant.id)
                text = f"(기술적 문제로 {participant.name}의 오프닝 발언을 가져오지 못했습니다)"
        return Utterance(
            round=0,
            speaker_id=participant.id,
            speaker_name=participant.name,
            content=text.strip(),
            timestamp=time.time(),
        )

    ai_participants = [p for p in config.participants if p.id != HUMAN_SPEAKER_ID]
    tasks = [_speak(p) for p in ai_participants]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    utterances = []
    for r in results:
        if isinstance(r, dict):
            utterances.append(r)
        elif isinstance(r, Exception):
            logger.warning("opening_speak_gather_exception: %s", r)

    # If human participant exists, route to human_turn first (before moderator)
    # by setting next_speaker_id + human_opening_pending flag
    if config.human_participant:
        human_opening = opening_map.get(HUMAN_SPEAKER_ID, "")
        if not human_opening:
            human_opening = f"{config.topic}에 대한 의견을 말씀해 주세요."
        return {
            "utterances": utterances,
            "current_round": 1,
            "phase": "discussing",
            "moderator_instruction": human_opening,
            "next_speaker_id": HUMAN_SPEAKER_ID,
            "human_opening_pending": True,
        }

    return {
        "utterances": utterances,
        "current_round": 1,
        "phase": "discussing",
        "moderator_instruction": "",
        "next_speaker_id": "",
    }
