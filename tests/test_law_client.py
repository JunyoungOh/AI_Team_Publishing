"""Unit tests for src.law.client — pure-function parsing + jo code conversion.

Network calls are not made: we test the normalisers and helpers directly
against fixture dicts that mirror law.go.kr's JSON response shape.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.law import client as client_mod
from src.law.cache import TTLCache
from src.law.client import LawClient, jo_to_code


# ── jo_to_code ──────────────────────────────────


@pytest.mark.parametrize(
    "inp,expected",
    [
        ("제38조", "003800"),
        ("제 38 조", "003800"),
        ("38", "003800"),
        ("제38조의2", "003802"),
        ("38조의 2", "003802"),
        ("1", "000100"),
        ("제1조의1", "000101"),
        ("38-2", "003802"),
    ],
)
def test_jo_to_code(inp, expected):
    assert jo_to_code(inp) == expected


def test_jo_to_code_empty():
    assert jo_to_code("") == ""
    assert jo_to_code("abc") == ""


# ── Parser normalisation ────────────────────────


def test_normalise_law_hit_builds_source_url():
    raw = {
        "법령명한글": "개인정보 보호법",
        "법령일련번호": "248613",
        "법령ID": "011357",
        "공포일자": "20240319",
        "소관부처명": "개인정보보호위원회",
    }
    out = client_mod._normalise_law_hit(raw)
    assert out["law_name"] == "개인정보 보호법"
    assert out["mst"] == "248613"
    assert out["promulgation_date"] == "20240319"
    assert out["source_url"].startswith("https://www.law.go.kr/")
    assert "개인정보 보호법" in out["source_url"]


def test_normalise_article_jo_code_and_label():
    raw = {
        "조문번호": "38",
        "조문가지번호": "0",
        "조문여부": "조문",
        "조문제목": "시정조치",
        "조문내용": "보호위원회는 개인정보처리자가 제15조제1항을 위반한 경우 ...",
    }
    out = client_mod._normalise_article(raw)
    assert out["jo_code"] == "003800"
    assert out["article_no"] == "제38조"
    assert "보호위원회" in out["text"]
    assert out["is_article"] is True


def test_normalise_article_branch():
    raw = {
        "조문번호": "15",
        "조문가지번호": "2",
        "조문여부": "조문",
        "조문제목": "",
        "조문내용": "…",
    }
    out = client_mod._normalise_article(raw)
    assert out["jo_code"] == "001502"
    assert out["article_no"] == "제15조의2"
    assert out["is_article"] is True


def test_normalise_article_rejects_chapter_by_type():
    raw = {
        "조문번호": "0",
        "조문가지번호": "0",
        "조문여부": "편장",
        "조문내용": "제3장 개인정보의 처리",
    }
    out = client_mod._normalise_article(raw)
    assert out["is_article"] is False


def test_normalise_article_rejects_chapter_by_pattern():
    raw = {
        "조문번호": "15",
        "조문가지번호": "0",
        "조문내용": "제3장 개인정보의 처리",
    }
    out = client_mod._normalise_article(raw)
    assert out["is_article"] is False


def test_get_law_filters_chapters():
    fake_payload = {
        "법령": {
            "기본정보": {"법령명한글": "개인정보 보호법", "공포일자": "20250401"},
            "조문": {
                "조문단위": [
                    {"조문번호": "0", "조문여부": "편장", "조문내용": "제1장 총칙"},
                    {"조문번호": "1", "조문여부": "조문", "조문내용": "제1조 목적..."},
                    {"조문번호": "14", "조문여부": "편장", "조문내용": "제3장 개인정보의 처리"},
                    {"조문번호": "15", "조문여부": "조문", "조문내용": "① 개인정보처리자는..."},
                ]
            },
        }
    }

    async def _run():
        with patch.object(client_mod.httpx, "AsyncClient", lambda timeout=None: _FakeClient(fake_payload)):
            c = LawClient(oc="testkey", timeout=5)
            return await c.get_law("270351")

    result = asyncio.run(_run())
    assert len(result["articles"]) == 2
    assert all(a["is_article"] for a in result["articles"])
    assert [a["jo_code"] for a in result["articles"]] == ["000100", "001500"]


def test_get_article_picks_real_article_over_chapter():
    fake_payload = {
        "법령": {
            "기본정보": {"법령명한글": "개인정보 보호법", "공포일자": "20250401"},
            "조문": {
                "조문단위": [
                    {"조문번호": "15", "조문여부": "편장", "조문내용": "제3장 개인정보의 처리"},
                    {
                        "조문번호": "15",
                        "조문여부": "조문",
                        "조문제목": "개인정보의 수집ㆍ이용",
                        "조문내용": "① 개인정보처리자는 다음 각 호...",
                    },
                ]
            },
        }
    }

    async def _run():
        with patch.object(client_mod.httpx, "AsyncClient", lambda timeout=None: _FakeClient(fake_payload)):
            c = LawClient(oc="testkey", timeout=5)
            return await c.get_article("270351", "001500")

    result = asyncio.run(_run())
    assert result["article"] is not None
    assert "개인정보처리자는" in result["article"]["text"]
    assert "제3장" not in result["article"]["text"]


def test_unwrap_single_root():
    payload = {"LawSearch": {"totalCnt": "3", "law": [{"법령명한글": "X"}]}}
    root = client_mod._unwrap_single_root(payload)
    assert root["totalCnt"] == "3"
    assert isinstance(root["law"], list)


def test_as_list_handles_scalar_and_none():
    assert client_mod._as_list(None) == []
    assert client_mod._as_list({"a": 1}) == [{"a": 1}]
    assert client_mod._as_list([1, 2]) == [1, 2]


# ── Client.search_law with mocked HTTP ───────────


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):  # noqa: ARG002
        return _FakeResponse(self._payload)


def test_search_law_normalises_results():
    fake_payload = {
        "LawSearch": {
            "totalCnt": "2",
            "law": [
                {
                    "법령명한글": "개인정보 보호법",
                    "법령일련번호": "248613",
                    "법령ID": "011357",
                    "공포일자": "20240319",
                    "소관부처명": "개인정보보호위원회",
                },
                {
                    "법령명한글": "정보통신망 이용촉진 및 정보보호 등에 관한 법률",
                    "법령일련번호": "111111",
                    "법령ID": "222222",
                    "공포일자": "20230101",
                    "소관부처명": "방송통신위원회",
                },
            ],
        }
    }

    async def _run():
        with patch.object(client_mod.httpx, "AsyncClient", lambda timeout=None: _FakeClient(fake_payload)):
            c = LawClient(oc="testkey", timeout=5)
            return await c.search_law("개인정보")

    result = asyncio.run(_run())
    assert result["total"] == 2
    assert len(result["results"]) == 2
    first = result["results"][0]
    assert first["law_name"] == "개인정보 보호법"
    assert first["mst"] == "248613"
    assert first["source_url"].startswith("https://www.law.go.kr/")


def test_search_law_empty_results_is_safe():
    fake_payload = {"LawSearch": {"totalCnt": "0", "law": None}}

    async def _run():
        with patch.object(client_mod.httpx, "AsyncClient", lambda timeout=None: _FakeClient(fake_payload)):
            c = LawClient(oc="testkey", timeout=5)
            return await c.search_law("없는법령")

    result = asyncio.run(_run())
    assert result["total"] == 0
    assert result["results"] == []


# ── TTLCache behaviour ──────────────────────────


def test_ttl_cache_hit_and_miss():
    cache = TTLCache(default_ttl=60)
    assert cache.get("x") is None
    cache.set("x", "value")
    assert cache.get("x") == "value"


def test_ttl_cache_expires():
    cache = TTLCache(default_ttl=60)
    cache.set("x", "value", ttl=-1)  # already expired
    assert cache.get("x") is None


def test_client_without_key_raises_clear_error():
    async def _run():
        c = LawClient(oc="", timeout=5)
        try:
            await c.search_law("x")
        except client_mod.LawAPIError as exc:
            return str(exc)
        return None

    msg = asyncio.run(_run())
    assert msg is not None and "law_oc" in msg
