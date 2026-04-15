"""run.command 생성기.

개발의뢰(최초개발/강화소)이 끝난 앱 디렉터리에 macOS 더블클릭으로 백엔드를 띄우고
브라우저를 여는 ``run.command`` 쉘 스크립트를 자동으로 작성한다.

핵심 아이디어: 정적 코드 파싱으로 포트를 알아내려 하지 않는다. 대신 생성된 쉘이
서버 stdout을 ``tee``로 받아내면서 ``localhost:NNNN`` 패턴을 실시간으로 grep해서
브라우저를 연다. Flask·FastAPI·Express·Vite·Next 등 어떤 프레임워크든 같은 방식으로 동작.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from src.utils.logging import get_logger

_logger = get_logger(agent_id="run_command_writer")

RUN_COMMAND_FILENAME = "run.command"


@dataclass(frozen=True)
class EntryGuess:
    label: str           # 사용자에게 보일 한글 설명 (예: "Flask 앱 (app.py)")
    command: str         # bash로 실행될 명령어 한 줄
    needs_python_venv: bool
    needs_node_install: bool
    fallback_port: int   # 로그 감지 실패 시 사용할 기본 포트


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _detect_python_entry(app_dir: Path) -> EntryGuess | None:
    """Python 엔트리 후보를 우선순위대로 검사."""
    candidates = ["app.py", "main.py", "server.py", "run.py", "wsgi.py", "manage.py"]
    for name in candidates:
        f = app_dir / name
        if not f.is_file():
            continue
        body = _read_text(f)
        # uvicorn/FastAPI 패턴 — module:app 추출 시도
        if "fastapi" in body.lower() or "uvicorn" in body.lower():
            module = name.rsplit(".", 1)[0]
            cmd = f'python -m uvicorn {module}:app --reload --port 8000'
            return EntryGuess(
                label=f"FastAPI 앱 ({name})",
                command=cmd,
                needs_python_venv=True,
                needs_node_install=False,
                fallback_port=8000,
            )
        # Django manage.py
        if name == "manage.py" and "django" in body.lower():
            return EntryGuess(
                label="Django 앱 (manage.py)",
                command="python manage.py runserver 8000",
                needs_python_venv=True,
                needs_node_install=False,
                fallback_port=8000,
            )
        # 일반 Python (Flask 포함)
        port_hint = 5050 if "flask" in body.lower() else 8000
        return EntryGuess(
            label=f"Python 앱 ({name})",
            command=f"python {name}",
            needs_python_venv=True,
            needs_node_install=False,
            fallback_port=port_hint,
        )
    return None


def _detect_node_entry(app_dir: Path) -> EntryGuess | None:
    """package.json 또는 일반 node 엔트리 검사."""
    pkg_path = app_dir / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(_read_text(pkg_path))
        except json.JSONDecodeError:
            pkg = {}
        scripts = (pkg.get("scripts") or {}) if isinstance(pkg, dict) else {}
        # dev > start > serve 우선순위 (개발 서버가 핫리로드 되니까)
        for script in ("dev", "start", "serve"):
            if script in scripts:
                return EntryGuess(
                    label=f"Node 앱 (npm run {script})",
                    command=f"npm run {script}",
                    needs_python_venv=False,
                    needs_node_install=True,
                    fallback_port=3000,
                )
    for name in ("server.js", "index.js", "app.js"):
        if (app_dir / name).is_file():
            return EntryGuess(
                label=f"Node 앱 ({name})",
                command=f"node {name}",
                needs_python_venv=False,
                needs_node_install=False,
                fallback_port=3000,
            )
    return None


def detect_entry(app_dir: Path) -> EntryGuess | None:
    """앱 디렉터리에서 가장 그럴듯한 백엔드 엔트리를 추측.

    package.json이 있으면 노드 우선, 없으면 Python 우선. 둘 다 있으면 둘 다 시도하고
    먼저 매칭되는 쪽 사용 (실무에서는 보통 한쪽만 있음).
    """
    if (app_dir / "package.json").is_file():
        node = _detect_node_entry(app_dir)
        if node:
            return node
    py = _detect_python_entry(app_dir)
    if py:
        return py
    return _detect_node_entry(app_dir)


def _render_script(guess: EntryGuess) -> str:
    """run.command 본문을 생성. heredoc/이스케이프 안 쓰는 단순 포맷."""
    venv_block = ""
    if guess.needs_python_venv:
        venv_block = (
            'if [ -f "requirements.txt" ] && [ ! -d "venv" ]; then\n'
            '  echo "📦 가상환경 생성 중 (최초 1회)..."\n'
            '  "$PY" -m venv venv\n'
            "fi\n"
            'if [ -d "venv" ]; then\n'
            "  # shellcheck disable=SC1091\n"
            '  source venv/bin/activate\n'
            "fi\n"
            'if [ -f "requirements.txt" ]; then\n'
            '  echo "📦 의존성 확인 중..."\n'
            '  pip install -q -r requirements.txt 2>/dev/null || true\n'
            "fi\n"
        )

    node_block = ""
    if guess.needs_node_install:
        node_block = (
            'if [ -f "package.json" ] && [ ! -d "node_modules" ]; then\n'
            '  echo "📦 npm 패키지 설치 중 (최초 1회)..."\n'
            "  npm install --silent\n"
            "fi\n"
        )

    return f"""#!/usr/bin/env bash
# run.command — Finder에서 더블클릭하면 백엔드 서버를 띄우고 브라우저를 엽니다.
# 개발의뢰이 작성한 파일입니다. 엔트리/포트가 틀리면 SERVER_CMD 줄을 수정하세요.
set -e
cd "$(dirname "$0")"

PY=${{PY:-python3}}
SERVER_CMD={json.dumps(guess.command)}    # 감지 결과: {guess.label}
FALLBACK_URL="http://localhost:{guess.fallback_port}"

echo "▶ 작업 폴더: $(pwd)"
echo "▶ 실행 명령: $SERVER_CMD"

{venv_block}{node_block}
LOGFILE="$(mktemp -t app_run_log.XXXXXX)"
trap 'rm -f "$LOGFILE"' EXIT

# 서버를 백그라운드로 띄우고 stdout을 LOGFILE로도 복사
( bash -c "$SERVER_CMD" 2>&1 | tee "$LOGFILE" ) &
SERVER_PID=$!

# 서버 출력에서 localhost URL 패턴을 최대 20초간 폴링
URL=""
for i in $(seq 1 40); do
  sleep 0.5
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    break
  fi
  URL=$(grep -oE "https?://(localhost|127\\.0\\.0\\.1)(:[0-9]+)?(/[^[:space:]]*)?" "$LOGFILE" | head -n1 || true)
  if [ -n "$URL" ]; then
    break
  fi
done

if [ -z "$URL" ]; then
  URL="$FALLBACK_URL"
  echo "⚠ 서버 URL을 자동 감지하지 못했어요. 기본값으로 시도합니다: $URL"
fi

echo "🌐 브라우저 열기: $URL"
open "$URL" 2>/dev/null || true

wait "$SERVER_PID"
"""


def write_run_command(app_dir: str | os.PathLike[str]) -> Path | None:
    """``app_dir`` 안에 ``run.command`` 작성. 엔트리 감지 실패 시 None 반환.

    이미 사용자가 직접 만든 run.command가 있어도 덮어쓴다 (개발의뢰 결과를 신뢰).
    """
    app_path = Path(app_dir)
    if not app_path.is_dir():
        _logger.warning("run_cmd_skip_not_dir", path=str(app_path))
        return None

    guess = detect_entry(app_path)
    if guess is None:
        _logger.info("run_cmd_skip_no_entry", path=str(app_path))
        return None

    target = app_path / RUN_COMMAND_FILENAME
    target.write_text(_render_script(guess), encoding="utf-8")
    # 더블클릭 가능하도록 실행 비트 설정
    current = target.stat().st_mode
    target.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    _logger.info(
        "run_cmd_written",
        path=str(target),
        entry=guess.label,
        command=guess.command,
    )
    return target
