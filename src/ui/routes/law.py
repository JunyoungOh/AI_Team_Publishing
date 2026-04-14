"""AI Law mode routes — WebSocket only."""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.config.settings import get_settings
from src.law.session import LawSession

router = APIRouter()


def _membership_enabled() -> bool:
    return get_settings().membership_enabled


def _verify_ws_token(ws: WebSocket) -> dict | None:
    from src.auth.security import verify_token

    token = ws.query_params.get("token", "")
    return verify_token(token) if token else None


@router.websocket("/ws/law")
async def law_endpoint(ws: WebSocket):
    """WebSocket endpoint for the AI Law mode."""
    await ws.accept()

    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "law_error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return

    user_id = user["sub"] if user else ""
    session = LawSession(ws, user_id=user_id)

    try:
        await session.run()
    except WebSocketDisconnect:
        session.cancel()
    except Exception as exc:  # noqa: BLE001
        try:
            await ws.send_json({"type": "law_error", "data": {"message": str(exc)}})
        except Exception:  # noqa: BLE001
            pass
        session.cancel()
