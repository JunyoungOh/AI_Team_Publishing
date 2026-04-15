"""Custom tool schemas + executors for DART (전자공시) mode.

Seven tools that wrap ``DartClient`` + ``CorpCodeIndex`` and expose the
narrow slice of Open DART functionality the LLM actually needs.

Tool list
---------
1. resolve_corp_code       — 회사명/종목코드 → corp_code 후보 랭킹
2. list_disclosures        — 공시 목록 (기간/유형 필터)
3. get_company             — 기업개황
4. get_document            — 공시서류 원문 (ZIP → XML → 텍스트, 요약자 모드용)
5. get_financial           — 재무제표 (fs_sections 배열 필터)
6. list_shareholder_reports — 대량보유(5%) + 임원/주요주주 지분 (통합)
7. list_dividend_events    — 배당에 관한 사항

Context is injected via functools.partial in engine.py — 각 세션은
자체 (client, caches, corp_index, verified_disclosures, citations)를 가짐.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from typing import Any, Awaitable, Callable
from xml.etree import ElementTree as ET

from src.config.settings import get_settings
from src.dart.cache import TTLCache
from src.dart.client import DartAPIError, DartClient
from src.dart.corp_code import CorpCodeIndex

logger = logging.getLogger(__name__)


# ── 섹션 코드 매핑 ──────────────────────────────

_VALID_SECTIONS = {"BS", "IS", "CIS", "CF", "SCE", "ALL"}


# ── Session context factory ────────────────────


def make_session_context() -> dict[str, Any]:
    """Build a fresh per-engine tool context."""
    settings = get_settings()
    return {
        "client": DartClient(),
        "search_cache": TTLCache(settings.dart_cache_ttl_search),
        "full_cache": TTLCache(settings.dart_cache_ttl_full),
        # CorpCodeIndex is a process-wide singleton, lazy-loaded on first call
        "corp_index": None,  # type: CorpCodeIndex | None
        # Verbatim guard: rcept_no values for disclosures we have actually fetched
        "verified_disclosures": set(),  # type: set[str]
        # Citation cards to push to the frontend after each turn
        "pending_citations": [],  # type: list[dict]
    }


async def _get_corp_index(ctx: dict[str, Any]) -> CorpCodeIndex:
    """Lazy-load the CorpCodeIndex singleton into session ctx."""
    if ctx["corp_index"] is None:
        ctx["corp_index"] = await CorpCodeIndex.get(ctx["client"])
    return ctx["corp_index"]


# ── Executors ─────────────────────────────────


async def _resolve_corp_code(
    ctx: dict[str, Any],
    *,
    query: str,
    limit: int = 5,
) -> str:
    query = (query or "").strip()
    if not query:
        return "Error: query(회사명 또는 6자리 종목코드)가 필요합니다."

    client: DartClient = ctx["client"]
    if not client.has_key:
        return "Error: dart_api_key가 설정되지 않았습니다. 관리자에게 문의하세요."

    try:
        index = await _get_corp_index(ctx)
    except DartAPIError as exc:
        return f"Error: CORPCODE.xml 부트스트랩 실패 — {exc}"

    hits = index.search(query, limit=max(1, min(20, limit)))
    payload = {
        "query": query,
        "total_index_size": index.size,
        "index_downloaded_at": index.downloaded_at,
        "results": hits,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _list_disclosures(
    ctx: dict[str, Any],
    *,
    corp_code: str | None = None,
    bgn_de: str | None = None,
    end_de: str | None = None,
    pblntf_ty: str | None = None,
    pblntf_detail_ty: str | None = None,
    page_no: int = 1,
    page_count: int = 20,
) -> str:
    cache: TTLCache = ctx["search_cache"]
    key = f"list::{corp_code or ''}::{bgn_de or ''}::{end_de or ''}::{pblntf_ty or ''}::{pblntf_detail_ty or ''}::{page_no}::{page_count}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: DartClient = ctx["client"]
    if not client.has_key:
        return "Error: dart_api_key가 설정되지 않았습니다."

    try:
        payload = await client.list_disclosures(
            corp_code=corp_code,
            bgn_de=bgn_de,
            end_de=end_de,
            pblntf_ty=pblntf_ty,
            pblntf_detail_ty=pblntf_detail_ty,
            page_no=page_no,
            page_count=page_count,
        )
    except DartAPIError as exc:
        return f"Error: Open DART 공시목록 조회 실패 — {exc}"

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _get_company(
    ctx: dict[str, Any],
    *,
    corp_code: str,
) -> str:
    corp_code = str(corp_code).strip()
    if not corp_code:
        return "Error: corp_code(8자리 고유번호)가 필요합니다."

    cache: TTLCache = ctx["full_cache"]
    key = f"company::{corp_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: DartClient = ctx["client"]
    try:
        payload = await client.get_company(corp_code)
    except DartAPIError as exc:
        return f"Error: Open DART 기업개황 조회 실패 — {exc}"

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _get_document(
    ctx: dict[str, Any],
    *,
    rcept_no: str,
    max_chars: int = 10000,
) -> str:
    """공시서류 원문 ZIP → XML → 텍스트 추출, 길이 제한.

    하이브리드 답변 전략에서 "요약자" 경로에서만 호출. 원문이 매우 클 수
    있어 기본 max_chars=10000 (약 2500 토큰)로 자름. LLM이 더 필요하면
    max_chars를 늘려 재호출.
    """
    rcept_no = str(rcept_no).strip()
    if not rcept_no:
        return "Error: rcept_no(공시접수번호)가 필요합니다."

    cache: TTLCache = ctx["full_cache"]
    # max_chars는 캐시 키에 넣지 않음 — 항상 풀텍스트를 캐시하고 슬라이스만 다르게
    key = f"doc_text::{rcept_no}"
    cached_text = cache.get(key)

    if cached_text is None:
        client: DartClient = ctx["client"]
        try:
            zip_bytes = await client.get_document(rcept_no)
        except DartAPIError as exc:
            return f"Error: Open DART 공시원문 조회 실패 — {exc}"
        try:
            cached_text = _extract_document_text(zip_bytes, rcept_no)
        except Exception as exc:  # noqa: BLE001
            return f"Error: 공시원문 파싱 실패 — {exc}"
        cache.set(key, cached_text)

    max_chars = max(500, min(50000, int(max_chars)))
    full_len = len(cached_text)
    truncated = full_len > max_chars
    snippet = cached_text[:max_chars]

    # Verbatim 가드에 등록
    ctx["verified_disclosures"].add(rcept_no)
    viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
    ctx["pending_citations"].append(
        {
            "rcept_no": rcept_no,
            "source_url": viewer_url,
            "length": full_len,
            "snippet_chars": len(snippet),
        }
    )

    payload = {
        "rcept_no": rcept_no,
        "total_length": full_len,
        "returned_length": len(snippet),
        "truncated": truncated,
        "text": snippet,
        "source_url": viewer_url,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _get_financial(
    ctx: dict[str, Any],
    *,
    corp_code: str,
    bsns_year: str,
    reprt_code: str = "11011",
    fs_sections: list[str] | None = None,
    fs_div: str = "OFS",
) -> str:
    corp_code = str(corp_code).strip()
    bsns_year = str(bsns_year).strip()
    reprt_code = str(reprt_code).strip()
    if not corp_code or not bsns_year or not reprt_code:
        return "Error: corp_code, bsns_year, reprt_code 가 모두 필요합니다."

    # fs_sections 기본값 + 정규화
    if not fs_sections:
        fs_sections = ["IS"]
    fs_sections = [s.upper() for s in fs_sections if isinstance(s, str)]
    invalid = [s for s in fs_sections if s not in _VALID_SECTIONS]
    if invalid:
        return (
            f"Error: 유효하지 않은 fs_sections 값 {invalid}. "
            f"허용값: BS, IS, CIS, CF, SCE, ALL"
        )

    cache: TTLCache = ctx["full_cache"]
    # 항상 전체 응답을 캐시 (섹션 필터는 응답에서 슬라이스)
    cache_key = f"fin::{corp_code}::{bsns_year}::{reprt_code}::{fs_div}"
    cached_full = cache.get(cache_key)

    if cached_full is None:
        client: DartClient = ctx["client"]
        try:
            full = await client.get_financial(
                corp_code=corp_code,
                bsns_year=bsns_year,
                reprt_code=reprt_code,
                fs_div=fs_div,
            )
        except DartAPIError as exc:
            return f"Error: Open DART 재무정보 조회 실패 — {exc}"
        cached_full = full
        cache.set(cache_key, cached_full)

    results = cached_full.get("results", [])
    if "ALL" in fs_sections:
        filtered = results
        section_label = "ALL"
    else:
        wanted = set(fs_sections)
        filtered = [r for r in results if r.get("sj_div") in wanted]
        section_label = "+".join(sorted(wanted))

    payload = {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
        "fs_div": fs_div,
        "fs_sections": fs_sections,
        "section_label": section_label,
        "total_before_filter": len(results),
        "returned": len(filtered),
        "results": filtered,
        "fetched_at": cached_full.get("fetched_at", ""),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _list_shareholder_reports(
    ctx: dict[str, Any],
    *,
    corp_code: str,
) -> str:
    """대량보유(5% 룰) + 임원/주요주주 지분 보고를 한 번에."""
    corp_code = str(corp_code).strip()
    if not corp_code:
        return "Error: corp_code가 필요합니다."

    cache: TTLCache = ctx["full_cache"]
    key = f"shareholders::{corp_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: DartClient = ctx["client"]
    major: dict[str, Any] = {"results": [], "error": None}
    executive: dict[str, Any] = {"results": [], "error": None}

    try:
        major = await client.list_major_holdings(corp_code)
    except DartAPIError as exc:
        major = {"results": [], "error": str(exc)}
    try:
        executive = await client.list_executive_holdings(corp_code)
    except DartAPIError as exc:
        executive = {"results": [], "error": str(exc)}

    payload = {
        "corp_code": corp_code,
        "major_holdings": major.get("results", []),
        "major_holdings_error": major.get("error"),
        "executive_holdings": executive.get("results", []),
        "executive_holdings_error": executive.get("error"),
        "fetched_at": major.get("fetched_at") or executive.get("fetched_at", ""),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


async def _list_dividend_events(
    ctx: dict[str, Any],
    *,
    corp_code: str,
    bsns_year: str,
    reprt_code: str = "11011",
) -> str:
    corp_code = str(corp_code).strip()
    bsns_year = str(bsns_year).strip()
    if not corp_code or not bsns_year:
        return "Error: corp_code와 bsns_year가 필요합니다."

    cache: TTLCache = ctx["full_cache"]
    key = f"dividend::{corp_code}::{bsns_year}::{reprt_code}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    client: DartClient = ctx["client"]
    try:
        payload = await client.list_dividend_events(
            corp_code=corp_code,
            bsns_year=bsns_year,
            reprt_code=reprt_code,
        )
    except DartAPIError as exc:
        return f"Error: Open DART 배당정보 조회 실패 — {exc}"

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    cache.set(key, rendered)
    return rendered


# ── XML → 텍스트 추출 유틸 ──────────────────


# DART 공시 ZIP 은 일반적으로 메인 본문 + 부속(감사보고서 등) 여러 XML 을 담음.
# 파일명 규칙: "{rcept_no}.xml" = 메인, "{rcept_no}_00NNN.xml" = 부속.
# XML 내부 ``<DOCUMENT-NAME ACODE="...">`` 코드로도 식별 가능:
#   11011 사업보고서 / 11012 반기 / 11013 1Q / 11014 3Q
#   00760 감사보고서 / 00761 연결감사보고서 등 부속 서류
import re as _re_main

_MAIN_REPORT_ACODE_RE = _re_main.compile(
    r'<DOCUMENT-NAME[^>]*ACODE="(\d{5})"', _re_main.IGNORECASE
)
_MAIN_REPORT_ACODES: set[str] = {"11011", "11012", "11013", "11014"}


def _pick_main_xml(zf: zipfile.ZipFile, rcept_no: str = "") -> str | None:
    """Pick the main filing XML from a DART document ZIP.

    The old implementation returned xml_names[0] which is whatever happens
    to be stored first — often the 감사보고서 (audit report) attachment
    instead of the 사업보고서 (main business report). Selection priority:

    1. Filename == ``{rcept_no}.xml`` (no suffix) — the main body
    2. First XML whose ``<DOCUMENT-NAME ACODE="...">`` is 11011-11014
    3. Fallback: first XML file
    """
    names = zf.namelist()
    xml_names = [n for n in names if n.lower().endswith(".xml")]
    if not xml_names:
        return None

    # Strategy 1: filename without suffix is the main body
    if rcept_no:
        target = f"{rcept_no}.xml"
        if target in xml_names:
            return target
        # Some ZIPs use nested paths — also check basename match
        for n in xml_names:
            if n.rsplit("/", 1)[-1] == target:
                return n

    # Strategy 2: peek ACODE header of each XML (read first 500 bytes only)
    for name in xml_names:
        try:
            with zf.open(name) as f:
                head = f.read(500).decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        m = _MAIN_REPORT_ACODE_RE.search(head)
        if m and m.group(1) in _MAIN_REPORT_ACODES:
            return name

    # Strategy 3: first XML (legacy fallback)
    return xml_names[0]


def _extract_document_text(zip_bytes: bytes, rcept_no: str = "") -> str:
    """Open DART 공시서류 ZIP → 메인 본문 XML 의 plain text.

    Picks the real main report (not the audit attachment) using filename
    and ACODE heuristics. For large XMLs (>1 MB) uses fast regex tag
    stripping; for smaller ones uses ElementTree for clean text+tail
    extraction.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        main_name = _pick_main_xml(zf, rcept_no)
        if not main_name:
            return ""
        xml_bytes = zf.read(main_name)

    # Big DART XMLs (사업보고서 can be 9+ MB) — regex stripping is ~100x
    # faster than ET.fromstring for files this large and the resulting
    # plain text is what we actually want to feed the LLM anyway.
    if len(xml_bytes) > 1_000_000:
        text = xml_bytes.decode("utf-8", errors="ignore")
        text = _re_main.sub(r"<[^>]+>", " ", text)
        text = _re_main.sub(r"\s+", " ", text).strip()
        return text

    # Small XMLs: use ElementTree for structure-preserving extraction
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        # DOCTYPE/entity issues — fallback to regex stripping
        text = xml_bytes.decode("utf-8", errors="ignore")
        text = _re_main.sub(r"<[^>]+>", " ", text)
        text = _re_main.sub(r"\s+", " ", text).strip()
        return text

    chunks: list[str] = []
    for elem in root.iter():
        if elem.text:
            t = elem.text.strip()
            if t:
                chunks.append(t)
        if elem.tail:
            t = elem.tail.strip()
            if t:
                chunks.append(t)
    return "\n".join(chunks)


# ── Schemas ───────────────────────────────────


DART_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "resolve_corp_code": {
        "name": "resolve_corp_code",
        "description": (
            "회사명 또는 6자리 종목코드를 Open DART의 8자리 corp_code(고유번호)로 해석합니다. "
            "회사명으로 질문을 받았다면 반드시 다른 도구를 호출하기 전에 이 도구를 먼저 사용하세요. "
            "동명이인/유사명 후보가 여러 개면 상위 5개가 반환되며 사용자에게 확인 요청할 수 있습니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "회사명(예: '삼성전자') 또는 6자리 종목코드(예: '005930')",
                },
                "limit": {
                    "type": "integer",
                    "description": "반환할 최대 후보 수 (기본 5, 최대 20)",
                },
            },
            "required": ["query"],
        },
    },
    "list_disclosures": {
        "name": "list_disclosures",
        "description": (
            "공시 목록을 검색합니다. corp_code를 지정하면 특정 회사의 공시만, 없으면 전체 "
            "회사 대상으로 기간·유형 필터만 적용됩니다. pblntf_ty: A=정기공시, B=주요사항, "
            "C=발행공시, D=지분공시, E=기타, F=외부감사. 연/분기 보고서 찾을 때는 pblntf_ty='A'. "
            "bgn_de/end_de를 생략하면 **자동으로 오늘 기준 12개월 범위**가 적용됩니다 — "
            "'최신 사업보고서' 같은 일반 질문에는 날짜를 생략하고 pblntf_ty='A'만 넘기는 게 가장 안전합니다. "
            "특정 연도를 원하면 bgn_de/end_de를 명시하세요(예: 2025년 전체 = bgn_de='20250101', end_de='20251231')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "corp_code": {
                    "type": "string",
                    "description": "8자리 고유번호 (선택 — 없으면 전체 회사 대상)",
                },
                "bgn_de": {"type": "string", "description": "시작일 YYYYMMDD (선택)"},
                "end_de": {"type": "string", "description": "종료일 YYYYMMDD (선택)"},
                "pblntf_ty": {
                    "type": "string",
                    "description": "공시유형 코드 A/B/C/D/E/F 등 (선택)",
                },
                "pblntf_detail_ty": {
                    "type": "string",
                    "description": "상세유형 코드 A001=사업보고서, A002=반기 등 (선택)",
                },
                "page_no": {"type": "integer", "description": "페이지 번호 (기본 1)"},
                "page_count": {
                    "type": "integer",
                    "description": "페이지당 건수 1-100 (기본 20)",
                },
            },
            "required": [],
        },
    },
    "get_company": {
        "name": "get_company",
        "description": (
            "기업개황 조회 — 회사명, 대표자, 업종, 상장구분, 주소, 법인등록번호, 사업자번호, "
            "IR URL 등. corp_code가 필수."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "corp_code": {"type": "string", "description": "8자리 고유번호"},
            },
            "required": ["corp_code"],
        },
    },
    "get_document": {
        "name": "get_document",
        "description": (
            "공시서류 원문 텍스트 — rcept_no로 실제 보고서 본문을 가져옵니다. 사용자가 "
            "'요약해줘', '자세히', '원문 확인' 등으로 원문 인용이 필요할 때만 사용하세요. "
            "텍스트는 기본 10000자로 잘립니다. 이 도구가 반환한 텍스트만을 원문 그대로 인용할 수 있습니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rcept_no": {
                    "type": "string",
                    "description": "공시접수번호 (list_disclosures 결과의 rcept_no)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "반환할 최대 문자 수 (기본 10000, 최대 50000)",
                },
            },
            "required": ["rcept_no"],
        },
    },
    "get_financial": {
        "name": "get_financial",
        "description": (
            "재무제표 조회 — fs_sections 배열로 원하는 섹션만 선택적으로 가져옵니다. "
            "IS=손익계산서(매출·영업이익·순이익), BS=재무상태표(자산·부채·자본), "
            "CF=현금흐름표, CIS=포괄손익계산서, SCE=자본변동표. "
            "교차 섹션 비율(ROE=순이익/자본, 재고자산회전율=매출/재고)은 fs_sections=['IS','BS']로 1회 호출. "
            "ALL은 사용자가 '종합', '전반', '건전성 평가' 등을 명시적으로 요청할 때만. "
            "불명확하면 기본값 ['IS']부터 시도."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "corp_code": {"type": "string", "description": "8자리 고유번호"},
                "bsns_year": {"type": "string", "description": "사업연도 YYYY (2015 이상)"},
                "reprt_code": {
                    "type": "string",
                    "description": "11011=사업(연간), 11012=반기, 11013=1Q, 11014=3Q (기본 11011)",
                },
                "fs_sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "반환할 섹션 배열. 허용값: BS, IS, CIS, CF, SCE, ALL. 기본 ['IS'].",
                },
                "fs_div": {
                    "type": "string",
                    "description": "CFS=연결, OFS=개별 (기본 OFS)",
                },
            },
            "required": ["corp_code", "bsns_year"],
        },
    },
    "list_shareholder_reports": {
        "name": "list_shareholder_reports",
        "description": (
            "대량보유(5% 룰) + 임원/주요주주 지분보고를 한 번에 조회. "
            "'누가 지분 많이 갖고 있어?', '임원 지분', '특수관계인' 류 질문에 사용."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "corp_code": {"type": "string", "description": "8자리 고유번호"},
            },
            "required": ["corp_code"],
        },
    },
    "list_dividend_events": {
        "name": "list_dividend_events",
        "description": (
            "배당에 관한 사항 — 특정 연도/보고서의 주당배당금, 배당성향, 배당수익률 등. "
            "사업보고서(11011) 기준이 일반적."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "corp_code": {"type": "string", "description": "8자리 고유번호"},
                "bsns_year": {"type": "string", "description": "사업연도 YYYY"},
                "reprt_code": {
                    "type": "string",
                    "description": "11011=사업(기본), 11012=반기, 11013=1Q, 11014=3Q",
                },
            },
            "required": ["corp_code", "bsns_year"],
        },
    },
}


DART_TOOL_EXECUTORS: dict[str, Callable[..., Awaitable[str]]] = {
    "resolve_corp_code": _resolve_corp_code,
    "list_disclosures": _list_disclosures,
    "get_company": _get_company,
    "get_document": _get_document,
    "get_financial": _get_financial,
    "list_shareholder_reports": _list_shareholder_reports,
    "list_dividend_events": _list_dividend_events,
}
