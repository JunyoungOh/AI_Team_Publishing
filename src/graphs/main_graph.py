"""Main graph assembly - single session pipeline with clarify branch.

Flow:
  [START] → intake → (strategy 있음 → single_session)
                   → (strategy 없음 → ceo_questions → await_user_answers → single_session)
          single_session → user_review_results → END
                         ↘ error_terminal → END

플레이북 경로: 저장된 플레이북이 이미 관점과 범위를 정의하므로 clarify 단계
없이 바로 실행. ad-hoc 경로: 모호한 태스크를 명확화 질문으로 구체화한 뒤
single_session에 전달. single_session_node가 태스크·전략·답변을 모두
받아 단일 CLI 세션으로 분석·보고서 작성까지 수행.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from src.engine import PipelineEngine

from src.graphs.nodes import (
    intake_node,
    ceo_questions_node,
    await_user_answers_node,
    user_review_results_node,
)
from src.graphs.nodes.single_session import single_session_node


def error_terminal_node(state: dict) -> dict:
    """Terminal node for error states.

    Generates a minimal error report so the user always gets a file,
    even when the pipeline crashes.
    """
    import os
    from src.utils import report_renderer

    error_msg = state.get("error_message", "Unknown error")
    session_id = state.get("session_id", "unknown")
    user_task = state.get("user_task", "")

    report_path = ""
    try:
        from src.utils.report_paths import build_report_dir
        report_dir = str(build_report_dir(user_task or "작업 실행 오류", session_id=session_id))
        html = report_renderer.render_partial_fallback(
            user_task=user_task or "작업 실행 오류",
            session_id=session_id,
            raw_text=str(error_msg),
            reason="pipeline_error",
            mode_label="Error Report",
        )
        with open(os.path.join(report_dir, "results.html"), "w", encoding="utf-8") as f:
            f.write(html)
        report_path = report_dir
    except Exception:
        pass

    return {
        "messages": [
            AIMessage(
                content=(
                    f"[System] Workflow terminated due to error.\n"
                    f"Error: {error_msg}"
                )
            )
        ],
        "phase": "error",
        "report_file_path": report_path,
    }


def _route_or_error(next_node: str):
    """Create a routing function that checks for error state before proceeding."""
    def router(state: dict) -> str:
        if state.get("phase") == "error":
            return "error_terminal"
        return next_node
    return router


def _route_after_intake(state: dict) -> str:
    """전략이 있으면 clarify 단계를 건너뛰고 바로 실행, 없으면 명확화 질문 생성.

    - 플레이북 경로: 저장된 플레이북이 이미 관점/범위/특별지시를 담고 있으므로
      generic clarify를 물어봐야 할 이유가 없다. 바로 single_session으로.
    - ad-hoc 경로: 사용자 지시가 모호할 수 있으므로 CEO가 명확화 질문을
      생성하고 답변을 받은 뒤 single_session으로.
    """
    if state.get("phase") == "error":
        return "error_terminal"
    pre_context = state.get("pre_context") or {}
    if pre_context.get("strategy"):
        return "single_session"
    return "ceo_questions"


def _build_engine() -> PipelineEngine:
    """Build the pipeline engine."""
    engine = PipelineEngine()

    engine.add_node("intake", intake_node)
    engine.add_node("ceo_questions", ceo_questions_node)
    engine.add_node("await_user_answers", await_user_answers_node)
    engine.add_node("single_session", single_session_node)
    engine.add_node("user_review_results", user_review_results_node)
    engine.add_node("error_terminal", error_terminal_node)

    engine.set_entry("intake")
    engine.set_router("intake", _route_after_intake)
    engine.set_router("ceo_questions", _route_or_error("await_user_answers"))
    engine.set_router("await_user_answers", _route_or_error("single_session"))
    engine.set_router("single_session", _route_or_error("user_review_results"))
    engine.set_router("user_review_results", lambda s: "__end__")
    engine.set_router("error_terminal", lambda s: "__end__")

    return engine


_CACHED_ENGINE: PipelineEngine | None = None
_engine_lock = __import__("threading").Lock()


def build_pipeline(checkpointer=None):
    """Build and compile the pipeline with a checkpointer."""
    global _CACHED_ENGINE
    if _CACHED_ENGINE is None:
        with _engine_lock:
            if _CACHED_ENGINE is None:
                _CACHED_ENGINE = _build_engine()
    return _CACHED_ENGINE.compile(checkpointer=checkpointer)
