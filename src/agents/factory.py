"""Agent factory for CEO creation.

CEO는 현재 Secretary 모드에서 도메인 라우팅·질문 생성 용도로만 사용된다.
메인 파이프라인(싱글 세션)은 에이전트 팩토리를 거치지 않고 Claude Code CLI를
직접 호출하므로 워커/리포터 팩토리는 제거되었다.
"""

from __future__ import annotations

from src.agents.ceo import CEOAgent
from src.config.settings import get_settings


def create_ceo() -> CEOAgent:
    return CEOAgent(agent_id="ceo-main-001", model=get_settings().ceo_model)
