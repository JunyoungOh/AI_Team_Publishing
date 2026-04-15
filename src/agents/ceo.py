"""CEO Agent — task routing and clarifying question generation.

현재 사용처는 Secretary 모드(`src/secretary/company_prep.py`)뿐이다. 메인
파이프라인은 싱글 CLI 세션으로 직접 실행되며 CEO 라우팅/질문 단계를 거치지
않는다.
"""

from __future__ import annotations

from src.agents.base import BaseAgent
from src.config.agent_registry import get_leader_domains, get_domain_description, get_worker_types
from src.config.personas import CEO_PERSONA, format_persona_block
from src.config.settings import get_settings
from src.models.messages import CEOGeneratedQuestions, CEORoutingDecision
from src.prompts.ceo_prompts import (
    GENERATE_ALL_QUESTIONS_SYSTEM,
    ROUTE_TASK_SYSTEM,
)


class CEOAgent(BaseAgent):
    """CEO that routes tasks and generates clarifying questions."""

    def __init__(self, agent_id: str, model: str = "opus") -> None:
        super().__init__(agent_id, model=model)
        self._persona_block = format_persona_block(CEO_PERSONA)
        self._settings = get_settings()

    def invoke(self, state: dict) -> dict:
        raise NotImplementedError("Use get_routing_decision or generate_all_questions")

    # CEO 라우팅/질문 단계에서 제외할 pre_context 키
    _PRE_CONTEXT_EXCLUDE = frozenset({
        "background", "escalation_policy", "default_answer", "domain_answers",
        "strategy", "output_format", "previous_report_path",
    })

    @staticmethod
    def _format_pre_context_block(state: dict) -> str:
        """Format pre_context into a prompt block for scheduled execution."""
        pre_ctx = state.get("pre_context", {})
        if not pre_ctx:
            return ""
        if set(pre_ctx.keys()) <= CEOAgent._PRE_CONTEXT_EXCLUDE:
            return ""
        lines = ["## 사전 제공 맥락 (Scheduled Mode)"]
        if pre_ctx.get("background"):
            lines.append(f"- 배경: {pre_ctx['background']}")
        if pre_ctx.get("escalation_policy"):
            lines.append(f"- 에스컬레이션 정책: {pre_ctx['escalation_policy']}")
        if pre_ctx.get("default_answer"):
            lines.append(f"- 기본 답변 방침: {pre_ctx['default_answer']}")
        for key, value in pre_ctx.items():
            if key not in CEOAgent._PRE_CONTEXT_EXCLUDE:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines) + "\n"

    def get_routing_decision(self, state: dict) -> CEORoutingDecision:
        """Return the full routing decision (used by Secretary's CompanyPrep).

        Uses ceo_route_model (sonnet by default) — routing is domain classification,
        not complex reasoning, so a lighter model is 5-10× faster with equal accuracy.
        """
        domains = get_leader_domains()
        domain_info = "\n".join(
            f"- {d}: {get_domain_description(d)}" for d in domains
        )
        system = self._format_prompt(
            ROUTE_TASK_SYSTEM,
            available_domains=domain_info,
            persona_block=self._persona_block,
            pre_context_block=self._format_pre_context_block(state),
        )
        from src.utils.parallel import run_async
        return run_async(
            self._bridge.structured_query(
                system_prompt=system,
                user_message=state["user_task"],
                output_schema=CEORoutingDecision,
                model=self._settings.ceo_route_model,
                allowed_tools=[],
                timeout=self._settings.planning_timeout,
                max_turns=self._settings.ceo_route_max_turns,
                effort=self._settings.ceo_routing_effort,
            )
        )

    def generate_all_questions(self, state: dict, selected_domains: list[str]) -> CEOGeneratedQuestions:
        """Generate clarifying questions for all selected domains in a single call."""
        domains_info = self._format_domains_info(selected_domains)
        system = self._format_prompt(
            GENERATE_ALL_QUESTIONS_SYSTEM,
            persona_block=self._persona_block,
            domains_info=domains_info,
            user_task=state["user_task"],
            pre_context_block=self._format_pre_context_block(state),
        )
        result: CEOGeneratedQuestions = self._query(
            system_prompt=system,
            user_content=state["user_task"],
            output_schema=CEOGeneratedQuestions,
            allowed_tools=[],
            timeout=self._settings.planning_timeout,
            max_turns=self._settings.ceo_max_turns,
            model=self._settings.ceo_route_model,
            effort=self._settings.ceo_question_effort,
        )
        self.logger.info(
            "all_questions_generated",
            domains=selected_domains,
            total=result.total_questions,
        )
        return result

    @staticmethod
    def _format_domains_info(selected_domains: list[str]) -> str:
        """Format domain descriptions and worker types for the question generation prompt."""
        parts = []
        for domain in selected_domains:
            desc = get_domain_description(domain)
            workers = get_worker_types(domain)
            workers_str = ", ".join(workers)
            parts.append(f"### {domain}\n- 설명: {desc}\n- 워커 유형: {workers_str}")
        return "\n\n".join(parts)
