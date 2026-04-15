"""Async HTTP client for the Korean law.go.kr Open API (DRF).

Covers the five endpoints we actually need:
- lawSearch.do?target=law   — 법령 검색
- lawService.do?target=law  — 법령 본문 (MST 기반)
- lawSearch.do?target=prec  — 판례 검색
- lawService.do?target=prec — 판례 본문 (ID 기반)
- lawSearch.do?target=expc  — 법령해석례 검색

Every result is normalised into ``{source_url, fetched_at, raw_id, ...}``
so downstream tool executors never lose the citation anchor.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.law.go.kr/DRF"
_LAW_HTML_BASE = "https://www.law.go.kr"


class LawAPIError(RuntimeError):
    """Raised when law.go.kr refuses or returns an unusable payload."""


class LawClient:
    """Thin async wrapper around law.go.kr DRF endpoints."""

    def __init__(self, oc: str | None = None, timeout: int | None = None) -> None:
        settings = get_settings()
        self._oc = oc if oc is not None else settings.law_oc
        self._timeout = timeout if timeout is not None else settings.law_request_timeout

    @property
    def has_key(self) -> bool:
        return bool(self._oc)

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._oc:
            raise LawAPIError("law_oc가 설정되지 않았습니다. .env에 LAW_OC를 추가하세요.")
        merged = {"OC": self._oc, "type": "JSON", **params}
        url = f"{_BASE_URL}/{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url, params=merged)
            except httpx.RequestError as exc:
                raise LawAPIError(f"network error: {exc}") from exc
        if resp.status_code != 200:
            raise LawAPIError(
                f"law.go.kr {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise LawAPIError(f"law.go.kr {path} returned non-JSON: {resp.text[:200]}") from exc

    # ── 법령 (law) ──────────────────────────────

    async def search_law(self, query: str, display: int = 10, page: int = 1) -> dict[str, Any]:
        raw = await self._get_json(
            "lawSearch.do",
            {"target": "law", "query": query, "display": display, "page": page},
        )
        root = _unwrap_single_root(raw)
        items = _as_list(root.get("law"))
        results = [_normalise_law_hit(item) for item in items]
        return {
            "query": query,
            "total": _as_int(root.get("totalCnt")),
            "results": results,
            "fetched_at": _now_iso(),
        }

    async def get_law(self, mst: str) -> dict[str, Any]:
        raw = await self._get_json(
            "lawService.do",
            {"target": "law", "MST": mst},
        )
        root = _unwrap_single_root(raw)
        basic = root.get("기본정보", {}) or {}
        articles_block = (root.get("조문", {}) or {}).get("조문단위", [])
        # Drop chapter/section/etc. headings so callers only see real articles.
        articles = [
            normalised
            for normalised in (_normalise_article(a) for a in _as_list(articles_block))
            if normalised.get("is_article")
        ]
        law_name = (
            basic.get("법령명_한글")
            or basic.get("법령명한글")
            or basic.get("법령명")
            or ""
        )
        law_id = str(basic.get("법령ID") or "").strip()
        return {
            "mst": mst,
            "law_name": law_name,
            "law_id": law_id,
            "promulgation_date": basic.get("공포일자", ""),
            "effective_date": basic.get("시행일자", ""),
            "articles": articles,
            "source_url": _build_law_url(law_name, basic.get("공포일자", ""), mst),
            "fetched_at": _now_iso(),
        }

    async def get_article(self, mst: str, jo: str) -> dict[str, Any]:
        """Fetch the full law body and return the single matching article."""
        law = await self.get_law(mst)
        target = jo.zfill(6)
        for art in law["articles"]:
            if art.get("jo_code", "").zfill(6) == target:
                return {
                    "mst": mst,
                    "law_name": law["law_name"],
                    "article": art,
                    "source_url": law["source_url"],
                    "fetched_at": law["fetched_at"],
                }
        return {
            "mst": mst,
            "law_name": law["law_name"],
            "article": None,
            "source_url": law["source_url"],
            "fetched_at": law["fetched_at"],
            "error": f"조문 번호 {jo}에 해당하는 내용이 없습니다.",
        }

    # ── 판례 (prec) ─────────────────────────────

    async def search_precedent(
        self, query: str, court: str | None = None, display: int = 10
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"target": "prec", "query": query, "display": display}
        if court:
            params["org"] = court
        raw = await self._get_json("lawSearch.do", params)
        root = _unwrap_single_root(raw)
        items = _as_list(root.get("prec"))
        return {
            "query": query,
            "total": _as_int(root.get("totalCnt")),
            "results": [_normalise_prec_hit(i) for i in items],
            "fetched_at": _now_iso(),
        }

    async def get_precedent(self, prec_id: str) -> dict[str, Any]:
        raw = await self._get_json(
            "lawService.do",
            {"target": "prec", "ID": prec_id},
        )
        root = _unwrap_single_root(raw)
        return {
            "prec_id": prec_id,
            "case_number": root.get("사건번호", ""),
            "case_name": root.get("사건명", ""),
            "court": root.get("법원명", ""),
            "decision_date": root.get("선고일자", ""),
            "summary": root.get("판시사항", ""),
            "holding": root.get("판결요지", ""),
            "full_text": root.get("판례내용", ""),
            "source_url": f"{_LAW_HTML_BASE}/LSW/precInfoP.do?precSeq={prec_id}",
            "fetched_at": _now_iso(),
        }

    # ── 법령해석례 (expc) ────────────────────────

    async def search_interpretation(self, query: str, display: int = 10) -> dict[str, Any]:
        raw = await self._get_json(
            "lawSearch.do",
            {"target": "expc", "query": query, "display": display},
        )
        root = _unwrap_single_root(raw)
        items = _as_list(root.get("expc"))
        return {
            "query": query,
            "total": _as_int(root.get("totalCnt")),
            "results": [_normalise_expc_hit(i) for i in items],
            "fetched_at": _now_iso(),
        }


# ── Normalisation helpers ───────────────────────


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _unwrap_single_root(payload: dict[str, Any]) -> dict[str, Any]:
    """law.go.kr wraps responses in a single top-level key — unwrap it."""
    if not isinstance(payload, dict) or not payload:
        return {}
    if len(payload) == 1:
        only = next(iter(payload.values()))
        if isinstance(only, dict):
            return only
    return payload


def _build_law_url(law_name: str, date: str, mst: str = "") -> str:
    """Build a canonical law.go.kr URL that actually renders content.

    The old ``/법령/{name}/{date}`` slug format returns HTTP 200 but only
    a 1.2KB empty stub page (white screen for the user) — law.go.kr removed
    that route without updating the redirect. The correct public detail
    page URL is ``/LSW/lsInfoP.do?lsiSeq={MST}`` which returns the full
    ~140KB law content.

    Important: ``lsiSeq`` takes the **MST (법령일련번호)**, NOT the 법령ID.
    They are different identifiers — e.g. 근로기준법 has law_id=001872 and
    MST=265959. Passing law_id=001872 resolves to "축우도살제한법" (a
    completely different law) because law_id and MST use overlapping
    numeric ranges. Always pass MST.

    Fallbacks when we don't have MST:
    - ``lsSc.do?query={name}`` — search page, always lands on something real
    - ``lsSc.do`` — bare search UI when even the name is missing
    """
    if mst:
        return f"{_LAW_HTML_BASE}/LSW/lsInfoP.do?lsiSeq={mst}"
    if law_name:
        return f"{_LAW_HTML_BASE}/lsSc.do?query={law_name}"
    return f"{_LAW_HTML_BASE}/lsSc.do"


def _normalise_law_hit(item: dict[str, Any]) -> dict[str, Any]:
    name = item.get("법령명한글") or item.get("법령명_한글") or item.get("법령명", "")
    mst = str(item.get("법령일련번호") or item.get("MST") or "").strip()
    law_id = str(item.get("법령ID", "")).strip()
    promulgation = item.get("공포일자", "")
    return {
        "law_name": name,
        "mst": mst,
        "law_id": law_id,
        "promulgation_date": promulgation,
        "effective_date": item.get("시행일자", ""),
        "ministry": item.get("소관부처명", ""),
        "source_url": _build_law_url(name, promulgation, mst),
    }


def _collect_paragraph_text(node: Any) -> list[str]:
    """Recursively collect all body text from 항/호/목 nesting.

    law.go.kr returns articles in a tiered structure:
        조문단위 → 항 (paragraph, ①②③) → 호 (item, 1.2.3.) → 목 (sub-item, 가나다)
    Simple articles have body in ``조문내용`` and no ``항``. Complex articles
    have ONLY the title in ``조문내용`` and the real body split across the
    ``항`` array. The old normalise function only read ``조문내용`` and missed
    every complex article — symptom: "근로기준법 제23조: title only, no body".
    """
    parts: list[str] = []
    if isinstance(node, dict):
        # Direct body fields at this level
        for key in ("항내용", "호내용", "목내용"):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        # Recurse into nested children
        for key in ("항", "호", "목"):
            child = node.get(key)
            if child is None:
                continue
            if isinstance(child, list):
                for c in child:
                    parts.extend(_collect_paragraph_text(c))
            else:
                parts.extend(_collect_paragraph_text(child))
    elif isinstance(node, list):
        for c in node:
            parts.extend(_collect_paragraph_text(c))
    return parts


def _normalise_article(item: dict[str, Any]) -> dict[str, Any]:
    jo_code = str(item.get("조문번호") or item.get("조문키") or "").strip()
    # 조문가지번호 handling: e.g. 제38조의2 → key "003802"
    branch = str(item.get("조문가지번호") or "0").strip() or "0"
    try:
        compound = f"{int(jo_code):04d}{int(branch):02d}"
    except (ValueError, TypeError):
        compound = jo_code.zfill(6)
    title = item.get("조문제목", "")
    # The title line — 조문내용 for simple articles is the full body, for
    # complex articles it's just "제23조(해고 등의 제한)" with the real body
    # split into paragraphs (항).
    title_line = (item.get("조문내용") or item.get("조문본문") or "").strip()
    paragraph_texts = _collect_paragraph_text(item)
    if paragraph_texts:
        # Complex article: assemble title line + all paragraphs
        full_text = title_line + "\n" + "\n".join(paragraph_texts) if title_line else "\n".join(paragraph_texts)
    else:
        # Simple article: title_line IS the full body
        full_text = title_line
    cleaned = _clean_text(full_text)
    # law.go.kr mixes 편/장/절/관 headings into the 조문단위 list alongside
    # actual articles. Two signals we use to skip them:
    # - "조문여부" field: "조문" = article, anything else (편장, 편장명, etc.) = heading
    # - content starting with "제N장"/"제N절" etc. (heading marker), no real body
    jo_type = (item.get("조문여부") or "").strip()
    is_article = True
    if jo_type and jo_type != "조문":
        is_article = False
    # Defensive pattern match: some laws omit 조문여부 entirely.
    import re as _re
    if _re.match(r"^\s*제\s*\d+\s*(편|장|절|관)", cleaned):
        is_article = False
    return {
        "jo_code": compound,
        "title": title,
        "article_no": _format_article_label(jo_code, branch),
        "text": cleaned,
        "is_article": is_article,
    }


def _normalise_prec_hit(item: dict[str, Any]) -> dict[str, Any]:
    prec_id = str(item.get("판례일련번호") or item.get("ID") or "").strip()
    return {
        "prec_id": prec_id,
        "case_name": item.get("사건명", ""),
        "case_number": item.get("사건번호", ""),
        "court": item.get("법원명", ""),
        "decision_date": item.get("선고일자", ""),
        "source_url": f"{_LAW_HTML_BASE}/LSW/precInfoP.do?precSeq={prec_id}" if prec_id else "",
    }


def _normalise_expc_hit(item: dict[str, Any]) -> dict[str, Any]:
    expc_id = str(item.get("법령해석례일련번호") or item.get("ID") or "").strip()
    return {
        "expc_id": expc_id,
        "title": item.get("안건명", ""),
        "agency": item.get("회신기관명", ""),
        "reply_date": item.get("회신일자", ""),
        "source_url": f"{_LAW_HTML_BASE}/LSW/expcInfoP.do?expcSeq={expc_id}" if expc_id else "",
    }


def _format_article_label(jo: str, branch: str) -> str:
    try:
        n = int(jo)
    except (ValueError, TypeError):
        return jo or ""
    if branch and branch != "0":
        return f"제{n}조의{branch}"
    return f"제{n}조"


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return str(value) if value is not None else ""
    # law.go.kr often inlines <br/> and literal "\n" markers
    return (
        value.replace("<br/>", "\n")
        .replace("<br>", "\n")
        .replace("\r\n", "\n")
        .strip()
    )


# ── Article-number helper (exposed for tools.py + tests) ──


def jo_to_code(article_text: str) -> str:
    """Convert user-typed article reference to 6-digit jo code.

    Examples::

        "제38조"       -> "003800"
        "38"           -> "003800"
        "제38조의2"    -> "003802"
        "38조의 2"     -> "003802"
    """
    import re

    if not article_text:
        return ""
    s = article_text.replace(" ", "")
    m = re.match(r"제?(\d+)조(?:의(\d+))?", s)
    if m:
        main = int(m.group(1))
        branch = int(m.group(2)) if m.group(2) else 0
        return f"{main:04d}{branch:02d}"
    m2 = re.match(r"(\d+)(?:-(\d+))?$", s)
    if m2:
        main = int(m2.group(1))
        branch = int(m2.group(2)) if m2.group(2) else 0
        return f"{main:04d}{branch:02d}"
    return ""
