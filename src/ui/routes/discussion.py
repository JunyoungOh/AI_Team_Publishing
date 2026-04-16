"""Discussion mode API routes — participant recommendation + file upload."""

from __future__ import annotations

import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

UPLOAD_ALLOWED_EXT = {".txt", ".md", ".pdf"}
UPLOAD_MAX_SIZE = 5 * 1024 * 1024
UPLOAD_MAX_TOTAL = 20 * 1024 * 1024
UPLOAD_BASE = Path("/tmp/disc-uploads")


def _sanitize_upload_filename(name: str) -> str:
    clean = Path(name).name
    return clean if clean else "upload"


def _validate_upload_extension(name: str) -> bool:
    return Path(name).suffix.lower() in UPLOAD_ALLOWED_EXT


async def _recommend_participants(
    topic: str,
    style: str,
    mode: str,
    count: int,
) -> dict:
    """Generate participant suggestions using Haiku model.

    Returns ``{"participants": [...], "human_suggestion": {...} | None}``.
    When *mode* is ``"participate"`` the response includes a suggested role
    for the real human user.
    """
    from src.utils.bridge_factory import get_bridge
    import json as _json

    include_human = mode == "participate"

    system_prompt = (
        "You are an expert at designing AI discussion participants. "
        "Given a topic, discussion style, and mode, generate diverse and compelling participants "
        "for an AI-powered discussion panel. Each participant should have a unique perspective."
    )

    if include_human:
        user_message = (
            f"Generate {count} AI discussion participants AND suggest ONE role "
            f"for a REAL HUMAN user who will also join the discussion.\n"
            f"Topic: {topic}\n"
            f"Discussion style: {style}\n\n"
            "The AI participants should have diverse, complementary perspectives.\n"
            "For the human suggestion, recommend a relatable role and stance "
            "a real person could naturally play — focus on lived experience "
            "rather than specialist expertise.\n\n"
            "Return ONLY a JSON object with no markdown:\n"
            '{"human_suggestion": {"name": "역할명", "persona": "입장·배경 설명"}, '
            '"participants": [{"name": "역할명", "persona": "관점·배경 설명"}, ...]}'
        )
    else:
        user_message = (
            f"Generate {count} discussion participants for the following:\n"
            f"Topic: {topic}\n"
            f"Discussion style: {style}\n"
            f"Mode: {mode}\n\n"
            "Return ONLY a JSON array with no markdown, no explanation. Each element must have:\n"
            '- "name": a descriptive role/title (e.g. "경제학자", "AI 전문가", "환경 운동가")\n'
            '- "persona": 1-2 sentence description of their viewpoint and background\n\n'
            "Example format:\n"
            '[{"name": "경제학자", "persona": "성장 중심의 시장경제를 옹호하며 규제 최소화를 주장합니다."}, '
            '{"name": "환경 전문가", "persona": "지속 가능한 발전을 강조하며 탄소 중립 정책을 지지합니다."}]'
        )

    def _sanitize_list(raw_list: list, limit: int) -> list[dict]:
        return [
            {"name": str(p.get("name", "")), "persona": str(p.get("persona", ""))}
            for p in raw_list
            if isinstance(p, dict)
        ][:limit]

    def _sanitize_human(raw: object) -> dict | None:
        if not isinstance(raw, dict):
            return None
        return {
            "name": str(raw.get("name", "")),
            "persona": str(raw.get("persona", "")),
        }

    try:
        bridge = get_bridge()
        raw = await bridge.raw_query(
            system_prompt=system_prompt,
            user_message=user_message,
            model="haiku",
            allowed_tools=[],
            timeout=30,
        )
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        parsed = _json.loads(text)

        if include_human and isinstance(parsed, dict):
            return {
                "participants": _sanitize_list(parsed.get("participants", []), count),
                "human_suggestion": _sanitize_human(parsed.get("human_suggestion")),
            }

        if isinstance(parsed, list):
            return {"participants": _sanitize_list(parsed, count), "human_suggestion": None}

    except Exception as e:
        import traceback
        print(f"[RECOMMEND] Failed: {type(e).__name__}: {e}")
        traceback.print_exc()

    defaults = [
        {"name": "전문가", "persona": "해당 분야의 전문 지식을 바탕으로 심층적인 분석을 제공합니다."},
        {"name": "비평가", "persona": "비판적 사고로 다양한 관점의 문제점을 지적합니다."},
        {"name": "혁신가", "persona": "창의적인 해결책과 새로운 접근 방식을 제안합니다."},
        {"name": "현실주의자", "persona": "실용적인 관점에서 현실 가능한 방안을 모색합니다."},
        {"name": "미래학자", "persona": "장기적 트렌드와 미래 시나리오를 분석합니다."},
    ]
    default_human = {"name": "토론 참여자", "persona": "주제에 대해 자유로운 시각으로 의견을 제시합니다."}
    return {
        "participants": defaults[:count],
        "human_suggestion": default_human if include_human else None,
    }


@router.post("/api/discussion/recommend-participants")
async def recommend_participants(request: Request):
    """Recommend discussion participants based on topic, style, and mode."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    topic = str(body.get("topic", "")).strip()
    style = str(body.get("style", "roundtable")).strip()
    mode = str(body.get("mode", "ai_discussion")).strip()
    count = int(body.get("count", 4))
    count = max(1, min(count, 8))

    result = await _recommend_participants(topic, style, mode, count)
    return JSONResponse(result)


@router.post("/api/discussion/upload")
async def discussion_upload(request: Request):
    """Upload files for persona cloning. Returns file paths."""
    form = await request.form()
    session_id = str(_uuid.uuid4())[:8]
    upload_dir = UPLOAD_BASE / session_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    total_size = 0

    for key in form:
        file = form[key]
        if not hasattr(file, "read"):
            continue

        filename = _sanitize_upload_filename(file.filename or "upload")
        if not _validate_upload_extension(filename):
            continue

        content = await file.read()
        total_size += len(content)
        if len(content) > UPLOAD_MAX_SIZE:
            continue
        if total_size > UPLOAD_MAX_TOTAL:
            break

        dest = upload_dir / filename
        if not str(dest.resolve()).startswith(str(upload_dir.resolve())):
            continue

        dest.write_bytes(content)
        uploaded.append({
            "file_id": f"{session_id}/{filename}",
            "filename": filename,
            "path": str(dest),
        })

    return {"files": uploaded, "session_id": session_id}
