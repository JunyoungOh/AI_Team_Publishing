"""skill_loader 단위 테스트.

핵심 검증:
  - 정상 케이스: SKILL.md + skill_metadata.json이 모두 있으면 컨텍스트 반환
  - required_mcps 기반 isolation_mode 결정
  - 누락된 SKILL.md → FileNotFoundError
  - path traversal slug → ValueError
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.skill_builder.registry import SkillRecord, SkillRegistry
from src.skill_builder.skill_loader import (
    IsolationMode,
    SkillExecutionContext,
    load_skill_for_execution,
)


def _seed_registry(tmp_path: Path, record: SkillRecord) -> Path:
    reg_path = tmp_path / "registry.json"
    SkillRegistry(path=reg_path).add(record)
    return reg_path


def _make_skill_dir(
    base: Path, slug: str, *, body: str, required_mcps: list[str]
) -> Path:
    skill_dir = base / f"skill-tab-{slug}"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    (skill_dir / "skill_metadata.json").write_text(
        json.dumps({"required_mcps": required_mcps}),
        encoding="utf-8",
    )
    return skill_dir


def test_load_skill_returns_context_for_isolated_skill(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(
        tmp_path, "summarize", body="# Summarize\n\n3줄 요약", required_mcps=[]
    )
    record = SkillRecord(
        slug="summarize",
        name="3줄 요약",
        skill_path=str(skill_dir),
        required_mcps=[],
        source="created",
        created_at="2026-04-09T00:00:00+00:00",
    )
    reg_path = _seed_registry(tmp_path, record)

    ctx = load_skill_for_execution("summarize", registry_path=reg_path)

    assert isinstance(ctx, SkillExecutionContext)
    assert ctx.slug == "summarize"
    assert ctx.skill_body == "# Summarize\n\n3줄 요약"
    assert ctx.isolation_mode == IsolationMode.ISOLATED
    assert ctx.required_mcps == []


def test_load_skill_returns_with_mcps_when_required(tmp_path: Path) -> None:
    skill_dir = _make_skill_dir(
        tmp_path, "websearch", body="# Web search", required_mcps=["serper"]
    )
    record = SkillRecord(
        slug="websearch",
        name="웹 검색",
        skill_path=str(skill_dir),
        required_mcps=["serper"],
        source="created",
        created_at="2026-04-09T00:00:00+00:00",
    )
    reg_path = _seed_registry(tmp_path, record)

    ctx = load_skill_for_execution("websearch", registry_path=reg_path)

    assert ctx.isolation_mode == IsolationMode.WITH_MCPS
    assert ctx.required_mcps == ["serper"]


def test_load_skill_raises_when_skill_md_missing(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill-tab-broken"
    skill_dir.mkdir()
    # SKILL.md 없음
    record = SkillRecord(
        slug="broken",
        name="망가진 스킬",
        skill_path=str(skill_dir),
        required_mcps=[],
        source="created",
        created_at="2026-04-09T00:00:00+00:00",
    )
    reg_path = _seed_registry(tmp_path, record)

    with pytest.raises(FileNotFoundError):
        load_skill_for_execution("broken", registry_path=reg_path)


def test_load_skill_raises_when_slug_not_in_registry(tmp_path: Path) -> None:
    reg_path = tmp_path / "registry.json"
    reg_path.write_text("[]", encoding="utf-8")

    with pytest.raises(KeyError):
        load_skill_for_execution("nope", registry_path=reg_path)


def test_load_skill_rejects_path_traversal_slug(tmp_path: Path) -> None:
    reg_path = tmp_path / "registry.json"
    reg_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError):
        load_skill_for_execution("../etc/passwd", registry_path=reg_path)


def test_load_skill_handles_missing_metadata_as_isolated(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill-tab-meta-less"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# X", encoding="utf-8")
    # skill_metadata.json 없음
    record = SkillRecord(
        slug="meta-less",
        name="메타 없음",
        skill_path=str(skill_dir),
        required_mcps=[],
        source="created",
        created_at="2026-04-09T00:00:00+00:00",
    )
    reg_path = _seed_registry(tmp_path, record)

    ctx = load_skill_for_execution("meta-less", registry_path=reg_path)
    assert ctx.isolation_mode == IsolationMode.ISOLATED
