"""Custom tool schemas + executors for AI Law mode.

Six tools that wrap ``LawClient`` and expose the narrow slice of
law.go.kr functionality the LLM actually needs.

The engine binds ``ctx`` via ``functools.partial`` so each engine
instance gets its own (client, search_cache, full_cache) triple —
no shared module-level state between sessions.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from src.config.settings import get_settings
from src.law.cache import TTLCache
from src.law.client import LawAPIError, LawClient, jo_to_code

logger = logging.getLogger(__name__)


# ── Session context factory ─────────────────────────────


def make_session_context() -> dict[str, Any]:
    """Build a fresh per-engine tool context."""
    settings = get_settings()
    return {
        "client": LawClient(),
        "search_cache": TTLCache(settings.law_cache_ttl_search),
        "full_cache": TTLCache(settings.law_cache_ttl_full),
        # Citations we have actually fetched the original text for — the
        # engine uses this set as an allow-list for its verbatim guard.
        "verified_articles": set(),  # type: set[tuple[str, str]]  (mst, jo_code)
        # Citation cards to push to the frontend after each turn.
        "pending_citations": [],     # type: list[dict]
    }


# ── Executors ───────────────────────────────────────────


async def _law_search(
    ctx: dict[str, Any],
    *,
    query: str,
    display: int = 10,
    page: int = 1,
) -> str:
    cache: TTLCache = ctx["search_cache"]
    key = f"law_search::{query}::{display}::{page}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: LawClient = ctx["client"]
    if not client.has_key:
        return "Error: law_oc(국가법령정보 Open API OC키)가 설정되지 않았습니다. 관리자에게 문의하세요."
    try:
        payload = await client.search_law(query, display=display, page=page)
    except LawAPIError as exc:
        return f"Error: law.go.kr 검색 실패 — {exc}"

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _law_get(
    ctx: dict[str, Any],
    *,
    mst: str,
) -> str:
    mst = str(mst).strip()
    if not mst:
        return "Error: mst(법령일련번호)가 필요합니다."
    cache: TTLCache = ctx["full_cache"]
    key = f"law_get::{mst}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: LawClient = ctx["client"]
    try:
        payload = await client.get_law(mst)
    except LawAPIError as exc:
        return f"Error: law.go.kr 본문 조회 실패 — {exc}"

    # Register every article as verified so the guard allows it,
    # even if the LLM later fetches a single article separately.
    verified = ctx["verified_articles"]
    for art in payload.get("articles", []):
        jo_code = art.get("jo_code", "")
        if jo_code:
            verified.add((mst, jo_code))

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _law_get_article(
    ctx: dict[str, Any],
    *,
    mst: str,
    jo: str,
) -> str:
    mst = str(mst).strip()
    if not mst or not jo:
        return "Error: mst와 jo(조문번호 또는 '제N조'형식)가 모두 필요합니다."

    jo_code = jo_to_code(jo) if not (jo.isdigit() and len(jo) == 6) else jo
    if not jo_code:
        return f"Error: 조문번호 '{jo}'을(를) 해석할 수 없습니다. 예) '제38조', '제38조의2', '38'"

    cache: TTLCache = ctx["full_cache"]
    key = f"law_get_article::{mst}::{jo_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: LawClient = ctx["client"]
    try:
        payload = await client.get_article(mst, jo_code)
    except LawAPIError as exc:
        return f"Error: law.go.kr 조문 조회 실패 — {exc}"

    if payload.get("article") is None:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # Allow-list this (mst, jo_code) for the verbatim guard + queue a
    # citation card for the frontend.
    ctx["verified_articles"].add((mst, jo_code))
    article = payload["article"]
    ctx["pending_citations"].append(
        {
            "law_name": payload.get("law_name", ""),
            "article_no": article.get("article_no", ""),
            "mst": mst,
            "jo_code": jo_code,
            "text": article.get("text", ""),
            "source_url": payload.get("source_url", ""),
        }
    )

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _prec_search(
    ctx: dict[str, Any],
    *,
    query: str,
    court: str | None = None,
    display: int = 10,
) -> str:
    cache: TTLCache = ctx["search_cache"]
    key = f"prec_search::{query}::{court or ''}::{display}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: LawClient = ctx["client"]
    try:
        payload = await client.search_precedent(query, court=court, display=display)
    except LawAPIError as exc:
        return f"Error: law.go.kr 판례 검색 실패 — {exc}"

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _prec_get(
    ctx: dict[str, Any],
    *,
    id: str,  # noqa: A002 — matches the tool input field
) -> str:
    prec_id = str(id).strip()
    if not prec_id:
        return "Error: id(판례일련번호)가 필요합니다."
    cache: TTLCache = ctx["full_cache"]
    key = f"prec_get::{prec_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: LawClient = ctx["client"]
    try:
        payload = await client.get_precedent(prec_id)
    except LawAPIError as exc:
        return f"Error: law.go.kr 판례 본문 조회 실패 — {exc}"

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _expc_search(
    ctx: dict[str, Any],
    *,
    query: str,
    display: int = 10,
) -> str:
    cache: TTLCache = ctx["search_cache"]
    key = f"expc_search::{query}::{display}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: LawClient = ctx["client"]
    try:
        payload = await client.search_interpretation(query, display=display)
    except LawAPIError as exc:
        return f"Error: law.go.kr 해석례 검색 실패 — {exc}"

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


# ── Schemas ─────────────────────────────────────────────


LAW_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "law_search": {
        "name": "law_search",
        "description": (
            "국가법령정보센터(law.go.kr)에서 키워드로 법령을 검색합니다. "
            "반환된 각 결과의 mst(법령일련번호)는 law_get_article 호출에 사용됩니다. "
            "반드시 조문 원문을 가져오기 전에 이 도구로 먼저 검색해야 합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드 (법령명/조문/주제)"},
                "display": {"type": "integer", "description": "한 페이지 결과 수 (기본 10)"},
                "page": {"type": "integer", "description": "페이지 번호 (기본 1)"},
            },
            "required": ["query"],
        },
    },
    "law_get": {
        "name": "law_get",
        "description": (
            "MST(법령일련번호)로 법령 전체 본문을 가져옵니다. "
            "조문 수가 매우 많을 수 있으므로, 특정 조문만 필요하면 law_get_article을 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mst": {"type": "string", "description": "법령일련번호(MST)"},
            },
            "required": ["mst"],
        },
    },
    "law_get_article": {
        "name": "law_get_article",
        "description": (
            "특정 법령의 특정 조문 원문을 가져옵니다. "
            "jo에는 '제38조', '제38조의2', '38', 또는 6자리 jo 코드 중 어느 것이든 넘길 수 있습니다. "
            "이 도구가 반환한 원문만을 그대로 인용할 수 있습니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mst": {"type": "string", "description": "법령일련번호(MST)"},
                "jo": {"type": "string", "description": "조문번호 (예: '제38조', '제38조의2', '38')"},
            },
            "required": ["mst", "jo"],
        },
    },
    "prec_search": {
        "name": "prec_search",
        "description": "판례를 검색합니다. 키워드와 선택적 법원명(court)으로 필터링할 수 있습니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드"},
                "court": {"type": "string", "description": "법원명 (선택)"},
                "display": {"type": "integer", "description": "결과 수 (기본 10)"},
            },
            "required": ["query"],
        },
    },
    "prec_get": {
        "name": "prec_get",
        "description": "판례일련번호(ID)로 판례 원문을 가져옵니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "판례일련번호"},
            },
            "required": ["id"],
        },
    },
    "expc_search": {
        "name": "expc_search",
        "description": "법령해석례(유권해석)를 검색합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드"},
                "display": {"type": "integer", "description": "결과 수 (기본 10)"},
            },
            "required": ["query"],
        },
    },
}


LAW_TOOL_EXECUTORS: dict[str, Callable[..., Awaitable[str]]] = {
    "law_search": _law_search,
    "law_get": _law_get,
    "law_get_article": _law_get_article,
    "prec_search": _prec_search,
    "prec_get": _prec_get,
    "expc_search": _expc_search,
}
