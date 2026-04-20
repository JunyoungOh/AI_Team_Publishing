#!/usr/bin/env python3
"""텔레그램 봇 알림 설정 검증 스크립트.

사용법:
    1) @BotFather에서 봇 만든 후 토큰을 .env의 TELEGRAM_BOT_TOKEN에 입력
    2) 만든 봇에게 텔레그램 앱에서 아무 메시지 하나 전송 (예: "/start")
    3) chat_id 모르면 먼저 `python3 scripts/test_telegram.py --lookup` 실행 → 내 chat_id 출력
    4) .env의 TELEGRAM_CHAT_ID에 입력
    5) `python3 scripts/test_telegram.py` 실행 → 테스트 메시지 수신 확인
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import httpx  # noqa: E402

from src.config.settings import get_settings  # noqa: E402
from src.utils.notifier import notify_completion  # noqa: E402


async def lookup_chat_id() -> None:
    settings = get_settings()
    token = settings.telegram_bot_token.strip()
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN이 .env에 없습니다.")
        sys.exit(1)
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
    data = resp.json()
    if not data.get("ok"):
        print(f"❌ getUpdates 실패: {data}")
        sys.exit(1)
    updates = data.get("result", [])
    if not updates:
        print("❌ 업데이트가 없습니다. 텔레그램 앱에서 봇에게 메시지 하나 보낸 후 다시 실행하세요.")
        sys.exit(1)
    seen: set[str] = set()
    print("발견된 chat_id 목록:")
    for upd in updates:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None or str(cid) in seen:
            continue
        seen.add(str(cid))
        name = chat.get("first_name") or chat.get("title") or ""
        print(f"  chat_id={cid}  ({name})")


async def send_test() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        print("❌ TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 .env에 없습니다.")
        print("   `python3 scripts/test_telegram.py --lookup`으로 chat_id 확인 후 .env 채우세요.")
        sys.exit(1)
    print("📨 테스트 메시지 전송 중...")
    await notify_completion(
        kind="skill",
        title="텔레그램 연동 테스트",
        summary="이 메시지가 보이면 설정 완료입니다.",
        duration_seconds=1.23,
        status="success",
    )
    print("✅ 전송 완료. 텔레그램 앱에서 메시지 확인하세요.")


def main() -> None:
    if "--lookup" in sys.argv:
        asyncio.run(lookup_chat_id())
    else:
        asyncio.run(send_test())


if __name__ == "__main__":
    main()
