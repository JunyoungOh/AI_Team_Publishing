"""Company Builder storage — agent & company JSON CRUD with path-safe isolation."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BASE_DIR = Path("data/users")


def _user_dir(user_id: str) -> Path:
    """Return the base directory for a user, creating it if needed."""
    safe_id = user_id or "_anonymous"
    d = _BASE_DIR / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path(base: Path, filename: str) -> Path:
    """Resolve a path and verify it stays under base (prevents traversal)."""
    resolved = (base / filename).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"Path traversal detected: {filename}")
    return resolved


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Agent CRUD ──


def save_agent(user_id: str, agent: dict[str, Any]) -> dict[str, Any]:
    """Save an agent. Generates id/created_at if missing. Returns the saved agent."""
    if not agent.get("id"):
        agent["id"] = _gen_id("agent")
    if not agent.get("created_at"):
        agent["created_at"] = _now_iso()

    d = _user_dir(user_id) / "agents"
    d.mkdir(exist_ok=True)
    path = _safe_path(d, f"{agent['id']}.json")
    path.write_text(json.dumps(agent, ensure_ascii=False, indent=2), encoding="utf-8")
    return agent


def load_agent(user_id: str, agent_id: str) -> dict[str, Any] | None:
    d = _user_dir(user_id) / "agents"
    path = _safe_path(d, f"{agent_id}.json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_agents(user_id: str) -> list[dict[str, Any]]:
    d = _user_dir(user_id) / "agents"
    if not d.exists():
        return []
    agents = []
    for f in sorted(d.glob("agent_*.json")):
        try:
            agents.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return agents


def delete_agent(user_id: str, agent_id: str) -> bool:
    d = _user_dir(user_id) / "agents"
    path = _safe_path(d, f"{agent_id}.json")
    if path.exists():
        path.unlink()
        return True
    return False


# ── Company CRUD ──


def save_company(user_id: str, company: dict[str, Any]) -> dict[str, Any]:
    """Save a company/team. Generates id/created_at if missing."""
    if not company.get("id"):
        company["id"] = _gen_id("company")
    if not company.get("created_at"):
        company["created_at"] = _now_iso()
    company["updated_at"] = _now_iso()

    d = _user_dir(user_id) / "companies"
    d.mkdir(exist_ok=True)
    path = _safe_path(d, f"{company['id']}.json")
    path.write_text(json.dumps(company, ensure_ascii=False, indent=2), encoding="utf-8")
    return company


def load_company(user_id: str, company_id: str) -> dict[str, Any] | None:
    d = _user_dir(user_id) / "companies"
    path = _safe_path(d, f"{company_id}.json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_companies(user_id: str) -> list[dict[str, Any]]:
    d = _user_dir(user_id) / "companies"
    if not d.exists():
        return []
    companies = []
    for f in sorted(d.glob("company_*.json")):
        try:
            companies.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return companies


def delete_company(user_id: str, company_id: str) -> bool:
    d = _user_dir(user_id) / "companies"
    path = _safe_path(d, f"{company_id}.json")
    if path.exists():
        path.unlink()
        return True
    return False


# ── Strategy CRUD (분석 전략 프리셋) ──


def save_strategy(user_id: str, strategy: dict[str, Any]) -> dict[str, Any]:
    """분석 전략 저장. id/created_at 자동 생성."""
    if not strategy.get("id"):
        strategy["id"] = _gen_id("strategy")
    if not strategy.get("created_at"):
        strategy["created_at"] = _now_iso()
    strategy["updated_at"] = _now_iso()

    d = _user_dir(user_id) / "strategies"
    d.mkdir(exist_ok=True)
    path = _safe_path(d, f"{strategy['id']}.json")
    path.write_text(json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
    return strategy


def load_strategy(user_id: str, strategy_id: str) -> dict[str, Any] | None:
    d = _user_dir(user_id) / "strategies"
    path = _safe_path(d, f"{strategy_id}.json")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_strategies(user_id: str) -> list[dict[str, Any]]:
    d = _user_dir(user_id) / "strategies"
    if not d.exists():
        return []
    strategies = []
    for f in sorted(d.glob("strategy_*.json")):
        try:
            strategies.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return strategies


def delete_strategy(user_id: str, strategy_id: str) -> bool:
    d = _user_dir(user_id) / "strategies"
    path = _safe_path(d, f"{strategy_id}.json")
    if path.exists():
        path.unlink()
        return True
    return False


