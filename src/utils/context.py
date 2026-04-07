"""Context management -- state slicing and token budgeting.

Each agent receives only the state slice it needs, not the entire state dict.
This reduces token consumption and prevents agents from being confused by
irrelevant data from other domains.
"""

from __future__ import annotations

from src.config.settings import get_settings as _get_settings


# ── Truncation ──────────────────────────────────────

def truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars with indicator."""
    if not text or len(text) <= max_chars:
        return text or ""
    return text[:max_chars] + f"\n... ({len(text) - max_chars} chars omitted)"


# ── State Slicers ──────────────────────────────────

def slice_for_ceo(state: dict) -> dict:
    """Extract CEO-relevant fields from state.

    CEO needs: user_task, workers summary (truncated plans/results), phase.
    CEO does NOT need: full worker plans/results, message history.
    """
    settings = _get_settings()
    workers_summary = []
    for w in state.get("workers", []):
        worker_slim = {
            "worker_id": w.get("worker_id", ""),
            "worker_domain": w.get("worker_domain", ""),
            "status": w.get("status", ""),
            "plan": truncate(w.get("plan", ""), settings.max_plan_chars_in_context),
            "execution_result": truncate(
                w.get("execution_result", ""), settings.max_result_chars_in_context
            ),
        }
        workers_summary.append(worker_slim)

    result = {
        "user_task": state.get("user_task", ""),
        "phase": state.get("phase", ""),
        "workers": workers_summary,
    }
    # Include prior rejection history so CEO sees its own NO_GO context on re-review
    if state.get("ceo_plan_confirmation"):
        result["ceo_plan_confirmation"] = state["ceo_plan_confirmation"]
    # Include pre_context for scheduled execution mode
    # strategy는 CEO에게 전달하지 않음 — 라우팅/질문은 지시사항만으로 수행
    if state.get("pre_context"):
        pre_ctx = state["pre_context"]
        slim = {k: v for k, v in pre_ctx.items() if k != "strategy"}
        if slim:
            result["pre_context"] = slim
    # Include iteration_counts for structured rejection history
    if state.get("iteration_counts"):
        result["iteration_counts"] = state["iteration_counts"]
    # Include CEO's own routing rationale (helps plan confirmation stay aligned)
    if state.get("ceo_routing_rationale"):
        result["ceo_routing_rationale"] = state["ceo_routing_rationale"]
    return result


def slice_for_reporter(state: dict) -> dict:
    """Extract reporter-relevant fields from state.

    Reporter only needs: user_task and workers with results/gap data.
    Reporter does NOT need: CEO confirmation history, iteration counts, pre_context.
    """
    settings = _get_settings()
    workers_summary = []
    for w in state.get("workers", []):
        worker_slim = {
            "worker_domain": w.get("worker_domain", ""),
            "status": w.get("status", ""),
            "execution_result": truncate(
                w.get("execution_result", ""), settings.max_result_chars_in_context
            ),
        }
        workers_summary.append(worker_slim)

    result = {
        "user_task": state.get("user_task", ""),
        "workers": workers_summary,
    }
    return result
