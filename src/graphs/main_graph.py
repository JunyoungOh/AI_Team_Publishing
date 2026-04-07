"""Main graph assembly - unified team-based pipeline.

Flow (P-E-S-R architecture):
  [START] → intake → ceo_route → ceo_questions → await_user_answers
          → ceo_task_decomposition → worker_execution (P-E-S-R loop inside)
          → ceo_final_report (Reporter: HTML formatting only)
          → user_review_results → END
          → worker_result_revision → ceo_final_report

P-E-S-R loop (inside worker_execution):
  Planner → Executor(s) → Synthesizer → Reviewer → (loop if FAIL)

Error handling: Any node can set phase="error", which routes to error_terminal → END.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from src.engine import PipelineEngine

from src.models.state import EnterpriseAgentState
from src.graphs.nodes import (
    intake_node,
    ceo_route_node,
    ceo_questions_node,
    await_user_answers_node,
    worker_execution_node,
    ceo_final_report_node,
    user_review_results_node,
    worker_result_revision_node,
)
from src.graphs.nodes.ceo_task_decomposition import ceo_task_decomposition_node
from src.graphs.nodes.single_session import single_session_node


def error_terminal_node(state: dict) -> dict:
    """Terminal node for error states.

    Generates a minimal error report so the user always gets a file,
    even when the pipeline crashes.
    """
    import os
    error_msg = state.get("error_message", "Unknown error")
    session_id = state.get("session_id", "unknown")
    user_task = state.get("user_task", "")

    # Generate emergency error report
    report_path = ""
    try:
        report_dir = os.path.join("data/reports", session_id)
        os.makedirs(report_dir, exist_ok=True)
        html = (
            f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            f"<title>Error Report</title></head><body>"
            f"<h1 style='color:#c0392b'>작업 실행 오류</h1>"
            f"<p><strong>작업:</strong> {user_task}</p>"
            f"<p><strong>오류:</strong> {error_msg}</p>"
            f"<p style='color:#888'>파이프라인 실행 중 오류가 발생하여 중단되었습니다.</p>"
            f"</body></html>"
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
    """싱글 세션 모드: CEO 라우팅 스킵 → 바로 질문 생성."""
    if state.get("phase") == "error":
        return "error_terminal"
    from src.config.settings import get_settings
    if get_settings().use_single_session:
        return "ceo_questions"
    return "ceo_route"


def _route_after_user_answers(state: dict) -> str:
    """싱글 세션 모드 vs 레거시 파이프라인 분기."""
    if state.get("phase") == "error":
        return "error_terminal"
    from src.config.settings import get_settings
    if get_settings().use_single_session:
        return "single_session"
    return "ceo_task_decomposition"


def _route_after_user_review(state: dict) -> str:
    if state.get("phase") == "error":
        return "error_terminal"
    if state.get("phase") == "worker_result_revision":
        return "worker_result_revision"
    return "__end__"


def _build_engine() -> PipelineEngine:
    """Build the enterprise agent PipelineEngine (uncompiled).

    Unified flow (same as builder mode, but ephemeral team):
      intake → ceo_route → ceo_questions → await_user_answers
      → ceo_task_decomposition → worker_execution
      → ceo_final_report → report_review → user_review_results → END
    """
    engine = PipelineEngine()

    # ── Register all nodes ──────────────────────────────
    engine.add_node("intake", intake_node)
    engine.add_node("ceo_route", ceo_route_node)
    engine.add_node("ceo_questions", ceo_questions_node)
    engine.add_node("await_user_answers", await_user_answers_node)
    engine.add_node("ceo_task_decomposition", ceo_task_decomposition_node)
    engine.add_node("worker_execution", worker_execution_node)
    engine.add_node("ceo_final_report", ceo_final_report_node)  # Reporter role (HTML formatting)
    engine.add_node("single_session", single_session_node)  # 싱글 CLI 세션 모드
    engine.add_node("user_review_results", user_review_results_node)
    engine.add_node("worker_result_revision", worker_result_revision_node)
    engine.add_node("error_terminal", error_terminal_node)

    # ── Entry + routers ────────────
    # P-E-S-R 루프는 worker_execution 내부에서 처리됨
    # report_review/ceo_report_revise 제거 — Reviewer가 루프 내에서 이미 검증 완료
    engine.set_entry("intake")
    engine.set_router("intake", _route_after_intake)
    engine.set_router("ceo_route", _route_or_error("ceo_questions"))
    engine.set_router("ceo_questions", _route_or_error("await_user_answers"))
    engine.set_router("await_user_answers", _route_after_user_answers)
    engine.set_router("ceo_task_decomposition", _route_or_error("worker_execution"))
    engine.set_router("single_session", _route_or_error("user_review_results"))
    engine.set_router("worker_execution", _route_or_error("ceo_final_report"))
    engine.set_router("ceo_final_report", _route_or_error("user_review_results"))
    engine.set_router("user_review_results", _route_after_user_review)
    engine.set_router("worker_result_revision", _route_or_error("ceo_final_report"))
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
