"""CEO Agent - task routing, plan confirmation, final report."""

from __future__ import annotations

from src.agents.base import BaseAgent
from src.config.agent_registry import get_leader_domains, get_domain_description, get_worker_types
from src.config.personas import CEO_PERSONA, format_persona_block
from src.config.settings import get_settings
from src.models.messages import (
    CEOGeneratedQuestions,
    CEOPlanConfirmation,
    CEORoutingDecision,
    FastPathResponse,
)
from src.prompts.ceo_prompts import (
    GENERATE_ALL_QUESTIONS_SYSTEM,
    ROUTE_TASK_SYSTEM,
)

# Legacy prompts — kept for backward compatibility but not used in current pipeline
CONFIRM_PLAN_SINGLE_SYSTEM = "Legacy prompt — not used in unified pipeline."
CONFIRM_PLAN_CROSS_SYSTEM = "Legacy prompt — not used in unified pipeline."
FAST_PATH_DIRECT_SYSTEM = "Legacy prompt — not used in unified pipeline."


class CEOAgent(BaseAgent):
    """CEO that routes tasks, confirms plans, and compiles final reports."""

    def __init__(self, agent_id: str, model: str = "opus") -> None:
        super().__init__(agent_id, model=model)
        self._persona_block = format_persona_block(CEO_PERSONA)
        self._settings = get_settings()

    def invoke(self, state: dict) -> dict:
        raise NotImplementedError("Use specific methods: get_routing_decision, confirm_plan")

    # CEO 라우팅/질문 단계에서 제외할 pre_context 키
    # strategy는 싱글 세션 실행 단계에서만 사용 (CEO 프롬프트에 주입하면 latency 폭증)
    _PRE_CONTEXT_EXCLUDE = frozenset({
        "background", "escalation_policy", "default_answer", "domain_answers",
        "strategy", "output_format", "previous_report_path", "output_mode",
    })

    @staticmethod
    def _format_pre_context_block(state: dict) -> str:
        """Format pre_context into a prompt block for scheduled execution.

        strategy는 제외 — CEO 라우팅/질문은 지시사항만 인식하면 됨.
        전략은 single_session 노드에서 실행 시점에 사용.
        """
        pre_ctx = state.get("pre_context", {})
        if not pre_ctx:
            return ""
        # strategy만 있는 경우 (나만의 방식 모드) → 블록 생략
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
        """Return the full routing decision (used by graph nodes).

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
        # Call bridge directly to override the default ceo_model with ceo_route_model.
        # allowed_tools=[] → no --allowedTools flag; opus handles structured output reliably.
        # effort="low" → CLI 2.1.68+: skip extended thinking for fast classification.
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
        """Generate clarifying questions for all selected domains in a single call.

        Replaces the leader_questions + ceo_optimize_questions two-step flow.
        """
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

    def generate_direct_response(
        self,
        state: dict,
        domain: str,
        rationale: str,
    ) -> FastPathResponse:
        """단순 작업에 대해 CEO가 직접 응답을 생성."""
        domain_desc = get_domain_description(domain)
        workers = get_worker_types(domain)
        system = self._format_prompt(
            FAST_PATH_DIRECT_SYSTEM,
            persona_block=self._persona_block,
            domain=domain,
            domain_description=domain_desc,
            available_workers=", ".join(workers),
            rationale=rationale,
            pre_context_block=self._format_pre_context_block(state),
        )
        return self._query(
            system_prompt=system,
            user_content=state["user_task"],
            output_schema=FastPathResponse,
            allowed_tools=[],
            timeout=self._settings.planning_timeout,
            max_turns=self._settings.ceo_max_turns,
        )

    def confirm_plan(self, state: dict) -> dict:
        """Review plans and give GO/NO_GO confirmation.

        Single domain  → simple confirmation mode (sonnet — faster).
        Multiple domains → cross-domain review mode (opus — deeper reasoning).
        Passes previous rejection history for context if NO_GO was given before.
        """
        workers = state.get("workers", [])
        domains = list({w.get("worker_domain", "") for w in workers})
        leader_count = len(domains) or 1
        user_task = state["user_task"]

        plans_summary = self._summarize_plans_from_workers(workers)
        rejection_history = self._format_rejection_history(state)

        if leader_count == 1:
            system = self._format_prompt(
                CONFIRM_PLAN_SINGLE_SYSTEM,
                user_task=user_task,
                persona_block=self._persona_block,
                rejection_history=rejection_history,
            )
            review_model = self._settings.confirm_plan_model_single
        else:
            rationale = state.get("ceo_routing_rationale", "")
            system = self._format_prompt(
                CONFIRM_PLAN_CROSS_SYSTEM,
                user_task=user_task,
                leader_count=leader_count,
                persona_block=self._persona_block,
                rejection_history=rejection_history,
                ceo_routing_rationale_block=rationale or "(라우팅 근거 없음)",
            )
            review_model = self._settings.confirm_plan_model_cross

        result: CEOPlanConfirmation = self._query(
            system_prompt=system,
            user_content=plans_summary,
            output_schema=CEOPlanConfirmation,
            allowed_tools=[],
            timeout=self._settings.ceo_review_timeout,
            max_turns=self._settings.ceo_max_turns,
            model=review_model,
            effort=self._settings.ceo_confirm_effort,
        )
        self.logger.info(
            "plan_confirmed",
            mode=result.review_mode,
            go_no_go=result.go_no_go,
        )
        return {
            "ceo_plan_confirmation": result.model_dump(),
            "phase": "worker_execution" if result.go_no_go == "GO" else "plan_review",
        }

    # ── Private helpers ──────────────────────────────────

    def _summarize_plans_from_workers(self, workers: list[dict]) -> str:
        """Summarize plans with truncation to avoid token explosion.

        Converts JSON plans to markdown for consistent readability.
        """
        from src.utils.plan_utils import format_plan_for_execution

        max_chars = self._settings.max_plan_chars_for_ceo
        parts = []
        for w in workers:
            plan_raw = w.get("plan", "(계획 없음)")
            plan_md = format_plan_for_execution(plan_raw)
            truncated = plan_md[:max_chars] + "..." if len(plan_md) > max_chars else plan_md
            parts.append(f"### [{w['worker_domain']}]\n{truncated}")
        return "\n".join(parts)

    @staticmethod
    def _format_rejection_history(state: dict) -> str:
        """Format previous NO_GO rejection as structured context for re-review."""
        prev = state.get("ceo_plan_confirmation")
        if not prev or prev.get("go_no_go") != "NO_GO":
            return ""
        msg = prev.get("confirmation_message", "")
        rejections = state.get("iteration_counts", {}).get("ceo_rejections", 0)

        lines = [
            "## 이전 거부 이력",
            f"- 거부 횟수: {rejections}회",
            f"- 거부 사유: {msg}",
        ]

        # Extract specific fix items from cross-domain analysis
        cross = prev.get("cross_domain_analysis")
        if cross:
            conflicts = cross.get("conflicts_found", [])
            if conflicts:
                lines.append("- 발견된 충돌:")
                for c in conflicts:
                    lines.append(f"  - {c}")
            deps = cross.get("dependencies_identified", [])
            if deps:
                lines.append("- 미반영 의존성:")
                for d in deps:
                    lines.append(f"  - {d}")

        lines.extend([
            "",
            "## 이번 검토 시 확인 필수 사항",
            "1. 위 거부 사유가 구체적으로 어떻게 해소되었는지 확인하세요.",
            "2. 해소되지 않은 항목이 있다면 NO_GO를 유지하세요.",
            "3. 해소되었다면 어떻게 해소되었는지 confirmation_message에 명시하세요.",
            "",
            "## ⚠️ NO_GO 비용 인식 (중요)",
            "NO_GO 1회 = 리더 재계획 3~5분 + CEO 재검토 7분 ≈ **10~12분 추가 소요**",
            f"현재까지 거부 횟수: {rejections}회 (누적 지연: 약 {rejections * 10}~{rejections * 12}분)",
            "워커 부하 과중(성공 기준 4+, 탐색 대상 4+, 검색 4+)이 아닌 한,",
            "계획의 방향성이 올바르다면 **GO를 선택하세요**.",
            "사소한 세부 조정은 워커 실행 중 자체적으로 해결됩니다.",
        ])
        return "\n".join(lines)
