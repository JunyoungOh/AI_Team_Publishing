"""워크스페이스 파일 목록/관리 API — 4개 모드 공유."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.utils.workspace import VALID_MODES, ensure_workspace, list_input_files

router = APIRouter()
WS_BASE = Path("data/workspace")


def _validate_mode(mode: str):
    if mode not in VALID_MODES:
        return JSONResponse(
            {"error": f"유효하지 않은 모드: {mode}"},
            status_code=400,
        )
    return None


@router.get("/api/workspace/{mode}/files")
async def workspace_list_files(mode: str):
    """모드의 input 폴더 내 파일 목록 반환."""
    err = _validate_mode(mode)
    if err:
        return err
    ensure_workspace(mode, base=WS_BASE)
    files = list_input_files(mode, base=WS_BASE)
    return {"files": files, "mode": mode}


@router.post("/api/workspace/{mode}/open")
async def workspace_open_folder(mode: str):
    """모드의 input 폴더를 OS 파일 탐색기로 열기."""
    err = _validate_mode(mode)
    if err:
        return err
    ws = ensure_workspace(mode, base=WS_BASE)
    folder = ws / "input"
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"opened": str(folder)}
