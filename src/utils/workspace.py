"""모드별 로컬 워크스페이스 폴더 관리 + 파일→AI 컨텍스트 변환.

폴더 구조:
  data/workspace/{mode}/input/   ← 사용자가 파일을 넣는 곳
  data/workspace/{mode}/output/  ← AI 결과물 저장
"""
from __future__ import annotations

from pathlib import Path

_DEFAULT_BASE = Path("data/workspace")

VALID_MODES = {"builder", "overtime", "skill"}

_TEXT_EXTENSIONS: set[str] = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".py", ".js", ".ts", ".html", ".css", ".sql",
}
_IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg", ".gif", ".svg"}
_MAX_TEXT_SIZE = 200 * 1024  # 200KB


def ensure_workspace(mode: str, base: Path | None = None) -> Path:
    """모드 워크스페이스 폴더를 생성하고 경로를 반환."""
    b = base or _DEFAULT_BASE
    ws = b / mode
    (ws / "input").mkdir(parents=True, exist_ok=True)
    (ws / "output").mkdir(parents=True, exist_ok=True)
    return ws


def list_input_files(mode: str, base: Path | None = None) -> list[dict]:
    """모드의 input 폴더 내 파일 목록을 반환."""
    b = base or _DEFAULT_BASE
    inp = b / mode / "input"
    if not inp.is_dir():
        return []
    return [
        {"name": f.name, "size": f.stat().st_size, "ext": f.suffix.lower()}
        for f in sorted(inp.iterdir())
        if f.is_file() and not f.name.startswith(".")
    ]


def _read_single_file(path: Path) -> str:
    """단일 파일을 AI 컨텍스트 문자열로 변환.

    절대경로를 항상 포함시켜, CLI 워커가 `Read` 도구로 첫 시도에 정확한
    경로를 짚을 수 있게 한다. 텍스트 파일은 내용도 함께 임베드해서 워커가
    굳이 Read를 호출하지 않아도 즉시 읽을 수 있다. 바이너리는 절대경로만
    제공되므로 워커가 Read/Bash로 직접 접근한다.
    """
    ext = path.suffix.lower()
    size = path.stat().st_size
    name = path.name
    abs_path = str(path)

    if ext in _TEXT_EXTENSIONS and size <= _MAX_TEXT_SIZE:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            return (
                f"[첨부파일: {name} ({size:,} bytes) — 절대경로: {abs_path}]\n"
                f"--- 파일 내용 ---\n{content}\n--- 파일 끝 ---"
            )
        except Exception:
            pass

    kind = "이미지" if ext in _IMAGE_EXTENSIONS else "바이너리"
    return (
        f"[첨부파일: {name} ({size:,} bytes, {kind} 파일) — 절대경로: {abs_path}]\n"
        f"이 파일은 위 절대경로로 `Read` 도구를 호출하면 직접 열어볼 수 있습니다."
    )


def read_files_as_context(
    mode: str,
    filenames: list[str],
    base: Path | None = None,
) -> str:
    """선택된 파일명 목록 → 통합 AI 컨텍스트 문자열.

    경로 순회 방지: input 폴더 직속 파일만 허용.
    """
    if not filenames:
        return ""
    b = base or _DEFAULT_BASE
    inp = (b / mode / "input").resolve()

    parts: list[str] = []
    for name in filenames:
        path = (inp / name).resolve()
        # 경로 순회 방지: 반드시 input 폴더의 직속 자식이어야 함
        if path.parent != inp:
            continue
        if path.exists() and path.is_file():
            parts.append(_read_single_file(path))

    if not parts:
        return ""
    return "\n\n## 사용자 첨부 파일\n\n" + "\n\n".join(parts)


def resolve_selected_paths(
    mode: str,
    filenames: list[str],
    base: Path | None = None,
) -> list[str]:
    """선택된 파일명 → `data/workspace/{mode}/input/{name}` 절대경로 문자열.

    파일 내용을 읽지 않고 **경로만** 반환한다. CLI가 `Read` 도구로 직접
    파일을 인지하도록 하는 방식 — 강화소(upgrade)의 cwd 기반 파일 인식과
    같은 철학이다.

    경로 순회 방지: input 폴더 직속 자식만 허용. 존재하지 않는 파일은
    조용히 건너뛴다.
    """
    if not filenames:
        return []
    b = base or _DEFAULT_BASE
    inp = (b / mode / "input").resolve()
    out: list[str] = []
    for name in filenames:
        path = (inp / name).resolve()
        if path.parent != inp:
            continue
        if path.exists() and path.is_file():
            out.append(str(path))
    return out


def get_output_dir(mode: str, session_id: str, base: Path | None = None) -> Path:
    """세션별 output 디렉토리를 생성하고 반환."""
    b = base or _DEFAULT_BASE
    out = b / mode / "output" / session_id
    out.mkdir(parents=True, exist_ok=True)
    return out
