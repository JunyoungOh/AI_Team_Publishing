"""완료 이벤트를 외부 채널(현재 Telegram)로 보내는 공용 알림 모듈.

설계 원칙:
- fire-and-forget: 네트워크/설정 오류로 원본 작업 흐름이 절대 막히지 않음
- adapter 패턴: 지금은 Telegram만, 추후 Kakao/Slack/Discord 어댑터를 채널 리스트에 추가만 하면 됨
- 호출부는 이벤트 '의도'만 표현(`notify_completion(kind, title, summary, ...)`) — 전송 채널은 모름
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import httpx

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


CompletionKind = Literal["skill", "dev", "scheduler", "foresight", "discussion"]
CompletionStatus = Literal["success", "failure", "timeout"]

_KIND_LABEL: dict[str, str] = {
    "skill": "플레이북",
    "dev": "개발의뢰",
    "scheduler": "자동실행",
    "foresight": "미래상상",
    "discussion": "토론",
}

_STATUS_EMOJI: dict[str, str] = {
    "success": "✅",
    "failure": "⚠️",
    "timeout": "⏱",
}


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}초"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}분 {sec}초"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분"


def _build_message(
    kind: CompletionKind,
    status: CompletionStatus,
    title: str,
    summary: str,
    duration_seconds: float | None,
) -> str:
    emoji = _STATUS_EMOJI.get(status, "✅")
    label = _KIND_LABEL.get(kind, kind)
    status_label = {"success": "완료", "failure": "실패", "timeout": "타임아웃"}[status]
    lines = [f"{emoji} *\\[{label}\\] {status_label}* — {title}"]
    if summary:
        lines.append(summary)
    dur = _format_duration(duration_seconds)
    if dur:
        lines.append(f"소요: {dur}")
    return "\n".join(lines)


async def _send_telegram(text: str) -> None:
    settings = get_settings()
    token = settings.telegram_bot_token.strip()
    chat_id = settings.telegram_chat_id.strip()
    if not token or not chat_id:
        logger.debug("Telegram notify skipped — token/chat_id not configured")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    timeout = settings.telegram_request_timeout
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning(
                "Telegram sendMessage failed (%s): %s",
                resp.status_code,
                resp.text[:200],
            )


async def notify_completion(
    kind: CompletionKind,
    title: str,
    summary: str = "",
    duration_seconds: float | None = None,
    status: CompletionStatus = "success",
) -> None:
    """완료 이벤트 알림을 구성된 모든 채널로 전송. 실패는 로그만 남기고 삼킴.

    호출부에서 await 하지 않고 ``asyncio.create_task(notify_completion(...))``로
    백그라운드 발사해도 되고, 원하면 await로 완료를 기다려도 된다 — 둘 다 안전.
    """
    settings = get_settings()
    if not settings.telegram_notify_enabled:
        return

    text = _build_message(kind, status, title, summary, duration_seconds)
    try:
        await _send_telegram(_escape_markdown_v2(text))
    except Exception as e:
        logger.warning("Telegram notify error (%s): %s", type(e).__name__, e)


def notify_completion_sync(
    kind: CompletionKind,
    title: str,
    summary: str = "",
    duration_seconds: float | None = None,
    status: CompletionStatus = "success",
) -> None:
    """동기 호출 지점(예: 스케줄러 콜백)용 래퍼. 이미 이벤트 루프가 돌고 있으면
    create_task로 백그라운드 발사, 아니면 asyncio.run으로 즉시 실행."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            notify_completion(kind, title, summary, duration_seconds, status)
        )
    except RuntimeError:
        try:
            asyncio.run(
                notify_completion(kind, title, summary, duration_seconds, status)
            )
        except Exception as e:
            logger.warning("Telegram notify (sync) error (%s): %s", type(e).__name__, e)


_MDV2_ESCAPE = set("_*[]()~`>#+-=|{}.!\\")


def _escape_markdown_v2(text: str) -> str:
    """MarkdownV2 금지문자를 이스케이프. `_build_message`가 만든 `*...*`(볼드)와
    `\\[`/`\\]`는 이미 의도된 제어 문자라 사전에 처리해둔 상태를 가정."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i : i + 2])
            i += 2
            continue
        if ch == "*":
            out.append(ch)
            i += 1
            continue
        if ch in _MDV2_ESCAPE:
            out.append("\\" + ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)
