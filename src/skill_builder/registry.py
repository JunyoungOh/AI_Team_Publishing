"""스킬 레지스트리 — data/skills/registry.json 단일 파일 저장소.

원자적 쓰기(임시 파일 + rename)로 부분 쓰기 인한 파일 손상을 방지합니다.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List


@dataclass
class SkillRecord:
    slug: str
    name: str
    skill_path: str
    required_mcps: List[str]
    source: str  # "created" | "imported"
    created_at: str  # ISO 8601


class SkillRegistry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def list_all(self) -> List[SkillRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        return [SkillRecord(**r) for r in raw]

    def add(self, record: SkillRecord) -> None:
        current = self.list_all()
        if any(r.slug == record.slug for r in current):
            raise ValueError(f"slug '{record.slug}' 이미 존재합니다")
        current.append(record)
        self._atomic_write([asdict(r) for r in current])

    def remove(self, slug: str) -> SkillRecord | None:
        """slug에 해당하는 레코드를 제거하고 반환. 없으면 None."""
        current = self.list_all()
        found = None
        remaining = []
        for r in current:
            if r.slug == slug:
                found = r
            else:
                remaining.append(r)
        if found is None:
            return None
        self._atomic_write([asdict(r) for r in remaining])
        return found

    def update(self, slug: str, *, name: str | None = None) -> SkillRecord | None:
        """slug에 해당하는 레코드의 필드를 갱신. 없으면 None."""
        current = self.list_all()
        target = None
        for r in current:
            if r.slug == slug:
                target = r
                break
        if target is None:
            return None
        if name is not None:
            target.name = name
        self._atomic_write([asdict(r) for r in current])
        return target

    def _atomic_write(self, data: list) -> None:
        """임시 파일 + rename으로 원자적 쓰기 (부분 쓰기 방지)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.path.parent),
            delete=False,
            suffix=".tmp",
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)
