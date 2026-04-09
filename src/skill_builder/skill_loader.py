"""스킬 실행 컨텍스트 로더.

실행 직전에 호출되어:
  1) registry에서 slug → SkillRecord 조회
  2) SKILL.md 본문 + skill_metadata.json 로드
  3) required_mcps 기반으로 격리 모드 결정

격리 모드는 execution_runner가 cwd / allowed_tools를 결정하는 데 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List

from src.skill_builder.registry import SkillRegistry


class IsolationMode(Enum):
    ISOLATED = "isolated"  # cwd=/tmp, builtin tools only
    WITH_MCPS = "with_mcps"  # cwd=project root, mcp__* tools allowed


@dataclass
class SkillExecutionContext:
    slug: str
    name: str
    skill_path: Path
    skill_body: str  # SKILL.md 전체 본문 (frontmatter 포함)
    required_mcps: List[str]
    isolation_mode: IsolationMode


def _validate_slug(slug: str) -> None:
    if not slug or "/" in slug or ".." in slug or "\\" in slug:
        raise ValueError(f"Invalid slug: {slug!r}")


def load_skill_for_execution(
    slug: str,
    *,
    registry_path: Path | None = None,
) -> SkillExecutionContext:
    """slug로 SkillExecutionContext를 만들어 반환.

    Raises:
        ValueError: slug가 유효하지 않음 (path traversal 등)
        KeyError: slug가 registry에 없음
        FileNotFoundError: SKILL.md가 디스크에 없음
    """
    _validate_slug(slug)

    reg_path = registry_path or Path("data/skills/registry.json")
    reg = SkillRegistry(path=reg_path)
    matching = [r for r in reg.list_all() if r.slug == slug]
    if not matching:
        raise KeyError(f"slug '{slug}' not found in registry")
    record = matching[0]

    skill_dir = Path(record.skill_path)
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found at {skill_md}")

    body = skill_md.read_text(encoding="utf-8")

    required_mcps = list(record.required_mcps or [])
    mode = IsolationMode.WITH_MCPS if required_mcps else IsolationMode.ISOLATED

    return SkillExecutionContext(
        slug=record.slug,
        name=record.name,
        skill_path=skill_dir,
        skill_body=body,
        required_mcps=required_mcps,
        isolation_mode=mode,
    )
