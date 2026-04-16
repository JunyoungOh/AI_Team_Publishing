"""Schedule storage — CRUD for heartbeat schedules (JSON per user)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BASE_DIR = Path("data/users")


def _user_schedule_dir(user_id: str) -> Path:
    safe_id = user_id or "_anonymous"
    d = _BASE_DIR / safe_id / "schedules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path(base: Path, filename: str) -> Path:
    resolved = (base / filename).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Path traversal detected: {filename}")
    return resolved


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    return f"schedule_{uuid.uuid4().hex[:12]}"


# ── Schedule CRUD ──


def _validate_cron(expr: str) -> bool:
    """Basic cron expression validation (5-field)."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    for part in parts:
        if not all(c in "0123456789*,-/" for c in part):
            return False
    return True


def save_schedule(user_id: str, schedule: dict[str, Any]) -> dict[str, Any]:
    """Save a schedule. Generates id/created_at if missing. Validates cron expression."""
    cron = schedule.get("cron_expression", "")
    if cron and not _validate_cron(cron):
        raise ValueError(f"Invalid cron expression: {cron}")
    if not schedule.get("id"):
        schedule["id"] = _gen_id()
    if not schedule.get("created_at"):
        schedule["created_at"] = _now_iso()
    schedule["updated_at"] = _now_iso()
    schedule.setdefault("user_id", user_id)
    schedule.setdefault("enabled", True)
    schedule.setdefault("run_history", [])
    schedule.setdefault("max_run_history", 30)
    schedule.setdefault("run_count", 0)

    d = _user_schedule_dir(user_id)
    path = _safe_path(d, f"{schedule['id']}.json")
    path.write_text(json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8")
    return schedule


def load_schedule(user_id: str, schedule_id: str) -> dict[str, Any] | None:
    d = _user_schedule_dir(user_id)
    path = _safe_path(d, f"{schedule_id}.json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_schedules(user_id: str) -> list[dict[str, Any]]:
    d = _user_schedule_dir(user_id)
    schedules = []
    for f in sorted(d.glob("schedule_*.json")):
        try:
            schedules.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return schedules


def delete_schedule(user_id: str, schedule_id: str) -> bool:
    d = _user_schedule_dir(user_id)
    path = _safe_path(d, f"{schedule_id}.json")
    if path.exists():
        path.unlink()
        return True
    return False


def toggle_schedule(user_id: str, schedule_id: str, enabled: bool) -> dict[str, Any] | None:
    """Enable or disable a schedule."""
    sched = load_schedule(user_id, schedule_id)
    if not sched:
        return None
    sched["enabled"] = enabled
    return save_schedule(user_id, sched)


def add_run_record(
    user_id: str,
    schedule_id: str,
    run_id: str,
    status: str = "running",
    report_path: str = "",
) -> dict[str, Any] | None:
    """Append a run record to schedule history. Trims to max_run_history."""
    sched = load_schedule(user_id, schedule_id)
    if not sched:
        return None

    record = {
        "run_id": run_id,
        "started_at": _now_iso(),
        "completed_at": None,
        "status": status,
        "report_path": report_path,
    }
    sched["run_history"].append(record)
    sched["last_run"] = record["started_at"]
    sched["run_count"] = sched.get("run_count", 0) + 1

    # Trim history
    max_h = sched.get("max_run_history", 30)
    if len(sched["run_history"]) > max_h:
        sched["run_history"] = sched["run_history"][-max_h:]

    return save_schedule(user_id, sched)


def update_run_record(
    user_id: str,
    schedule_id: str,
    run_id: str,
    status: str = "completed",
    report_path: str = "",
) -> dict[str, Any] | None:
    """Update an existing run record's status and completion time."""
    sched = load_schedule(user_id, schedule_id)
    if not sched:
        return None

    for rec in sched["run_history"]:
        if rec["run_id"] == run_id:
            rec["status"] = status
            rec["completed_at"] = _now_iso()
            if report_path:
                rec["report_path"] = report_path
            break

    return save_schedule(user_id, sched)
