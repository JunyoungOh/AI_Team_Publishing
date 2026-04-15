"""Async HTTP client for Open DART (전자공시) API.

Covers the endpoints needed by the DART mode tools:
- list.json                  — 공시검색
- company.json               — 기업개황
- document.xml               — 공시서류 원문 (ZIP 내부 XML)
- corpCode.xml               — 고유번호 (ZIP 내부 XML, bulk)
- fnlttSinglAcntAll.json     — 단일회사 전체 재무제표
- majorstock.json            — 대량보유 상황보고
- elestock.json              — 임원·주요주주 소유보고
- dvSnd.json                 — 배당/증자 등 주요사항 (여기서는 배당만)

Design notes:
- Open DART는 `crtfc_key` 쿼리 파라미터로 인증 (헤더 X)
- status 000 = 정상, 나머지는 에러 코드 (013 = 데이터 없음 등)
- document.xml, corpCode.xml은 ZIP 바이트를 반환 — 호출자가 압축 해제
- 응답 JSON은 평평한 구조이므로 law.go.kr처럼 루트 언래핑이 필요 없음
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://opendart.fss.or.kr/api"
_DART_VIEWER_BASE = "https://dart.fss.or.kr/dsaf001/main.do"

# Open DART status 코드 — 000은 정상, 나머지는 설명과 함께 예외로 변환
_OK_STATUS = "000"
_STATUS_MESSAGES: dict[str, str] = {
    "010": "등록되지 않은 키입니다.",
    "011": "사용할 수 없는 키입니다.",
    "012": "접근할 수 없는 IP입니다.",
    "013": "조회된 데이터가 없습니다.",
    "014": "파일이 존재하지 않습니다.",
    "020": "요청 제한 횟수를 초과했습니다.",
    "021": "조회 가능한 회사 개수가 초과했습니다.",
    "100": "필드의 부적절한 값입니다.",
    "101": "부적절한 접근입니다.",
    "800": "시스템 점검 중입니다.",
    "900": "정의되지 않은 오류가 발생했습니다.",
    "901": "사용자 계정의 개인정보 보유기간이 만료되었습니다.",
}


class DartAPIError(RuntimeError):
    """Raised when Open DART refuses or returns an unusable payload."""


class DartClient:
    """Thin async wrapper around Open DART endpoints."""

    def __init__(self, api_key: str | None = None, timeout: int | None = None) -> None:
        settings = get_settings()
        self._key = api_key if api_key is not None else settings.dart_api_key
        self._timeout = timeout if timeout is not None else settings.dart_request_timeout

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._key:
            raise DartAPIError(
                "dart_api_key가 설정되지 않았습니다. .env에 DART_API_KEY를 추가하세요."
            )
        merged = {"crtfc_key": self._key, **params}
        url = f"{_BASE_URL}/{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url, params=merged)
            except httpx.RequestError as exc:
                raise DartAPIError(f"network error: {exc}") from exc
        if resp.status_code != 200:
            raise DartAPIError(
                f"Open DART {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DartAPIError(
                f"Open DART {path} returned non-JSON: {resp.text[:200]}"
            ) from exc
        # status 000 = 정상, 013 = 데이터 없음 (에러 아님, 빈 결과)
        status = str(payload.get("status", ""))
        if status == _OK_STATUS:
            return payload
        if status == "013":
            # 데이터 없음은 예외가 아니라 빈 리스트로 정상 반환
            payload.setdefault("list", [])
            return payload
        msg = payload.get("message") or _STATUS_MESSAGES.get(status, "알 수 없는 오류")
        raise DartAPIError(f"Open DART {path} status={status}: {msg}")

    async def _get_bytes(self, path: str, params: dict[str, Any]) -> bytes:
        """바이너리(ZIP) 다운로드용 — corpCode.xml, document.xml 등."""
        if not self._key:
            raise DartAPIError(
                "dart_api_key가 설정되지 않았습니다. .env에 DART_API_KEY를 추가하세요."
            )
        merged = {"crtfc_key": self._key, **params}
        url = f"{_BASE_URL}/{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url, params=merged)
            except httpx.RequestError as exc:
                raise DartAPIError(f"network error: {exc}") from exc
        if resp.status_code != 200:
            raise DartAPIError(
                f"Open DART {path} returned HTTP {resp.status_code}"
            )
        # Open DART는 에러 시 JSON으로 응답 — Content-Type으로 판별
        ctype = resp.headers.get("content-type", "").lower()
        if "json" in ctype or resp.content[:1] == b"{":
            try:
                payload = resp.json()
            except ValueError:
                return resp.content
            status = str(payload.get("status", ""))
            if status and status != _OK_STATUS:
                msg = payload.get("message") or _STATUS_MESSAGES.get(status, "알 수 없는 오류")
                raise DartAPIError(f"Open DART {path} status={status}: {msg}")
        return resp.content

    # ── 공시검색 (list.json) ──────────────────────

    async def list_disclosures(
        self,
        corp_code: str | None = None,
        bgn_de: str | None = None,
        end_de: str | None = None,
        pblntf_ty: str | None = None,
        pblntf_detail_ty: str | None = None,
        page_no: int = 1,
        page_count: int = 20,
    ) -> dict[str, Any]:
        """공시 목록 조회.

        Args:
            corp_code: 8자리 고유번호. None이면 전체 회사 대상
            bgn_de: 시작일 YYYYMMDD
            end_de: 종료일 YYYYMMDD
            pblntf_ty: 공시유형 (A=정기공시, B=주요사항, C=발행, D=지분, E=기타, F=외부감사 등)
            pblntf_detail_ty: 상세유형 (A001=사업보고서, A002=반기보고서 등)
            page_no: 페이지 번호
            page_count: 페이지당 건수 (1-100)
        """
        # Open DART list.json 은 bgn_de 가 누락되면 빈 배열을 반환한다 (empirical).
        # 호출자(또는 LLM)가 날짜를 넘기지 않았을 때도 "최신 N년" 이 자동 조회되도록
        # 오늘 기준 12개월 범위를 디폴트로 채운다. 둘 중 하나만 있으면 나머지를 보완.
        bgn_de, end_de = _normalise_date_range(bgn_de, end_de)

        params: dict[str, Any] = {
            "page_no": str(page_no),
            "page_count": str(max(1, min(100, page_count))),
            "bgn_de": bgn_de,
            "end_de": end_de,
        }
        if corp_code:
            params["corp_code"] = corp_code
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty
        if pblntf_detail_ty:
            params["pblntf_detail_ty"] = pblntf_detail_ty

        raw = await self._get_json("list.json", params)
        items = raw.get("list", []) or []
        return {
            "total_count": _as_int(raw.get("total_count")),
            "total_page": _as_int(raw.get("total_page")),
            "page_no": _as_int(raw.get("page_no")),
            "page_count": _as_int(raw.get("page_count")),
            "results": [_normalise_disclosure(i) for i in items],
            "fetched_at": _now_iso(),
        }

    # ── 기업개황 (company.json) ────────────────────

    async def get_company(self, corp_code: str) -> dict[str, Any]:
        raw = await self._get_json("company.json", {"corp_code": corp_code})
        return {
            "corp_code": corp_code,
            "corp_name": raw.get("corp_name", ""),
            "corp_name_eng": raw.get("corp_name_eng", ""),
            "stock_name": raw.get("stock_name", ""),
            "stock_code": raw.get("stock_code", ""),
            "ceo_nm": raw.get("ceo_nm", ""),
            "corp_cls": raw.get("corp_cls", ""),  # Y=유가 K=코스닥 N=코넥스 E=기타
            "jurir_no": raw.get("jurir_no", ""),
            "bizr_no": raw.get("bizr_no", ""),
            "adres": raw.get("adres", ""),
            "hm_url": raw.get("hm_url", ""),
            "ir_url": raw.get("ir_url", ""),
            "phn_no": raw.get("phn_no", ""),
            "fax_no": raw.get("fax_no", ""),
            "induty_code": raw.get("induty_code", ""),
            "est_dt": raw.get("est_dt", ""),
            "acc_mt": raw.get("acc_mt", ""),
            # dsae001/selectPopup.do 는 DART 내부 팝업 경유 URL이라 직접 열면 404.
            # dsab007/main.do 는 공시검색 페이지 — 회사코드로 직접 필터할 수 없지만
            # 안전하게 로드되며 사용자가 회사명으로 다시 검색할 수 있다.
            "source_url": "https://dart.fss.or.kr/dsab007/main.do",
            "fetched_at": _now_iso(),
        }

    # ── 공시서류 원문 (document.xml, ZIP) ─────────

    async def get_document(self, rcept_no: str) -> bytes:
        """공시서류 원문 ZIP 바이트 반환 — 호출자가 zipfile로 풀어서 XML 파싱."""
        return await self._get_bytes("document.xml", {"rcept_no": rcept_no})

    # ── 고유번호 (corpCode.xml, ZIP) ───────────────

    async def get_corp_code_zip(self) -> bytes:
        """CORPCODE.xml ZIP 바이트 — 회사 전체 ↔ corp_code 매핑."""
        return await self._get_bytes("corpCode.xml", {})

    # ── 재무정보 (fnlttSinglAcntAll.json) ─────────

    async def get_financial(
        self,
        corp_code: str,
        bsns_year: str,
        reprt_code: str,
        fs_div: str = "OFS",
    ) -> dict[str, Any]:
        """단일회사 전체 재무제표.

        Args:
            corp_code: 고유번호
            bsns_year: 사업연도 (YYYY, 2015 이상)
            reprt_code: 보고서코드 (11011=사업, 11012=반기, 11013=1분기, 11014=3분기)
            fs_div: CFS=연결, OFS=개별 (기본 개별)
        """
        raw = await self._get_json(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
                "fs_div": fs_div,
            },
        )
        items = raw.get("list", []) or []
        return {
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "fs_div": fs_div,
            "results": [_normalise_financial(i) for i in items],
            "fetched_at": _now_iso(),
        }

    # ── 대량보유 상황보고 (majorstock.json) ────────

    async def list_major_holdings(self, corp_code: str) -> dict[str, Any]:
        """5% 룰 대량보유 보고."""
        raw = await self._get_json("majorstock.json", {"corp_code": corp_code})
        items = raw.get("list", []) or []
        return {
            "corp_code": corp_code,
            "results": [_normalise_major_holding(i) for i in items],
            "fetched_at": _now_iso(),
        }

    # ── 임원/주요주주 소유 (elestock.json) ─────────

    async def list_executive_holdings(self, corp_code: str) -> dict[str, Any]:
        """임원·주요주주 소유보고."""
        raw = await self._get_json("elestock.json", {"corp_code": corp_code})
        items = raw.get("list", []) or []
        return {
            "corp_code": corp_code,
            "results": [_normalise_executive_holding(i) for i in items],
            "fetched_at": _now_iso(),
        }

    # ── 배당 (dvSnd.json) ─────────────────────────

    async def list_dividend_events(
        self, corp_code: str, bsns_year: str, reprt_code: str = "11011"
    ) -> dict[str, Any]:
        """배당에 관한 사항."""
        raw = await self._get_json(
            "alotMatter.json",
            {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
            },
        )
        items = raw.get("list", []) or []
        return {
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "results": [_normalise_dividend(i) for i in items],
            "fetched_at": _now_iso(),
        }


# ── Normalisation helpers ───────────────────────


def _normalise_date_range(
    bgn_de: str | None,
    end_de: str | None,
) -> tuple[str, str]:
    """Fill + auto-correct bgn_de/end_de to compensate for LLM training-cutoff bias.

    Two problems this solves:

    1. **Open DART requires bgn_de**: list.json returns an empty array when
       bgn_de is missing. So we always provide both values.

    2. **LLM passes stale end_de**: Claude's training cutoff is ~mid-2025, so
       even with "today is 2026-04-15" in the system prompt, it often anchors
       date ranges on training-time dates (e.g. bgn_de=20240101, end_de=20251231).
       This silently misses 2026 filings.

       Heuristic: if the caller's end_dt is in the past but **within 365 days
       of today**, the intent is "recent filings" with wrong anchoring → snap
       end_dt forward to today and expand bgn_dt if it becomes too narrow.
       If end_dt is **more than 365 days behind today**, it's a genuine
       historical query (e.g. "삼성전자 2018 사업보고서") → leave untouched.

    Resulting behaviour:
    - No dates passed → today − 365d ~ today (12mo recent window)
    - Partial range (recent intent) → end snaps to today, bgn extended if needed
    - Partial range (historical, end > 365d old) → preserved as-is
    """
    today = datetime.now()
    recent_threshold = timedelta(days=365)
    min_window = timedelta(days=365)

    def _parse(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y%m%d")
        except ValueError:
            return None

    end_dt = _parse(end_de)
    bgn_dt = _parse(bgn_de)

    # Case 1: end_de explicitly given and in the recent past → LLM likely
    # meant "up to now" but anchored on training cutoff. Snap forward.
    if end_dt is not None:
        age = today - end_dt
        if timedelta(0) < age < recent_threshold:
            end_dt = today

    # Case 2: no end_de at all → default to today.
    if end_dt is None:
        end_dt = today

    # Now fill bgn_dt. If bgn_dt is None → 12 months before end_dt.
    # If bgn_dt given but the window is narrower than min_window AND we just
    # snapped end_dt forward, expand bgn_dt to keep a useful window.
    if bgn_dt is None:
        bgn_dt = end_dt - min_window
    elif (end_dt - bgn_dt) < min_window and end_dt == today:
        # We extended end to today → widen bgn so the window is not tiny.
        bgn_dt = end_dt - min_window

    # Guardrail: bgn_dt never after end_dt.
    if bgn_dt > end_dt:
        bgn_dt = end_dt - min_window

    return bgn_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")


def _as_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _viewer_url(rcept_no: str) -> str:
    if not rcept_no:
        return _DART_VIEWER_BASE
    return f"{_DART_VIEWER_BASE}?rcpNo={rcept_no}"


def _normalise_disclosure(item: dict[str, Any]) -> dict[str, Any]:
    rcept_no = str(item.get("rcept_no", "")).strip()
    return {
        "corp_code": item.get("corp_code", ""),
        "corp_name": item.get("corp_name", ""),
        "stock_code": item.get("stock_code", ""),
        "corp_cls": item.get("corp_cls", ""),
        "report_nm": item.get("report_nm", ""),
        "rcept_no": rcept_no,
        "flr_nm": item.get("flr_nm", ""),
        "rcept_dt": item.get("rcept_dt", ""),
        "rm": item.get("rm", ""),
        "source_url": _viewer_url(rcept_no),
    }


def _normalise_financial(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_nm": item.get("account_nm", ""),
        "account_id": item.get("account_id", ""),
        "sj_div": item.get("sj_div", ""),  # BS/IS/CIS/CF/SCE — 섹션 필터용
        "sj_nm": item.get("sj_nm", ""),    # 재무제표명 (재무상태표, 손익계산서 등)
        "thstrm_nm": item.get("thstrm_nm", ""),
        "thstrm_amount": item.get("thstrm_amount", ""),
        "frmtrm_nm": item.get("frmtrm_nm", ""),
        "frmtrm_amount": item.get("frmtrm_amount", ""),
        "bfefrmtrm_nm": item.get("bfefrmtrm_nm", ""),
        "bfefrmtrm_amount": item.get("bfefrmtrm_amount", ""),
        "currency": item.get("currency", ""),
    }


def _normalise_major_holding(item: dict[str, Any]) -> dict[str, Any]:
    rcept_no = str(item.get("rcept_no", "")).strip()
    return {
        "rcept_no": rcept_no,
        "repror": item.get("repror", ""),  # 보고자
        "stkqy": item.get("stkqy", ""),  # 보유주식수
        "stkqy_irds": item.get("stkqy_irds", ""),  # 증감
        "stkrt": item.get("stkrt", ""),  # 보유비율
        "stkrt_irds": item.get("stkrt_irds", ""),
        "report_tp": item.get("report_tp", ""),
        "report_resn": item.get("report_resn", ""),
        "source_url": _viewer_url(rcept_no),
    }


def _normalise_executive_holding(item: dict[str, Any]) -> dict[str, Any]:
    rcept_no = str(item.get("rcept_no", "")).strip()
    return {
        "rcept_no": rcept_no,
        "repror": item.get("repror", ""),
        "isu_exctv_rgist_at": item.get("isu_exctv_rgist_at", ""),
        "isu_exctv_ofcps": item.get("isu_exctv_ofcps", ""),
        "isu_main_shrholdr": item.get("isu_main_shrholdr", ""),
        "sp_stock_lmp_cnt": item.get("sp_stock_lmp_cnt", ""),
        "sp_stock_lmp_irds_cnt": item.get("sp_stock_lmp_irds_cnt", ""),
        "sp_stock_lmp_rate": item.get("sp_stock_lmp_rate", ""),
        "source_url": _viewer_url(rcept_no),
    }


def _normalise_dividend(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "se": item.get("se", ""),  # 구분 (유상증자 등)
        "thstrm": item.get("thstrm", ""),
        "frmtrm": item.get("frmtrm", ""),
        "lwfr": item.get("lwfr", ""),
        "stock_knd": item.get("stock_knd", ""),
    }
