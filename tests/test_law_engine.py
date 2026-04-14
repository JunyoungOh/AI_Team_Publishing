"""Unit tests for src.law.engine — pure heuristics only (no bridge calls)."""
from __future__ import annotations

from src.law.engine import (
    LawEngine,
    _DISCLAIMER,
    _strip_json_echo,
    _sanitise_urls,
    _find_balanced_end,
    _parse_cli_response,
    _strip_toolu_markers,
    _strip_tool_result_labels,
)


# ── _looks_final ──────────────────────────────────


def test_looks_final_detects_disclaimer():
    text = "짧은 답변이지만\n\n" + _DISCLAIMER
    assert LawEngine._looks_final(text) is True


def test_looks_final_rejects_empty():
    assert LawEngine._looks_final("") is False


def test_looks_final_rejects_long_answer_without_disclaimer():
    """Length alone must NOT trigger early termination — that was the bug
    that suppressed citation cards when the LLM echoed JSON before fetching
    the article."""
    text = "## 제15조\n\n" + ("본문 " * 300) + " 제15조 내용 ..."
    assert LawEngine._looks_final(text) is False


def test_looks_final_rejects_short_without_disclaimer():
    assert LawEngine._looks_final("개인정보보호법을 검색하겠습니다.") is False


# ── _find_balanced_end ────────────────────────────


def test_find_balanced_end_simple_object():
    text = '{"key": "value"}'
    assert _find_balanced_end(text, 0) == len(text) - 1


def test_find_balanced_end_nested_array():
    text = '[{"a": 1}, {"b": [2, 3]}]'
    assert _find_balanced_end(text, 0) == len(text) - 1


def test_find_balanced_end_respects_string_brackets():
    text = '{"msg": "has ] inside"}'
    assert _find_balanced_end(text, 0) == len(text) - 1


def test_find_balanced_end_unbalanced_returns_negative():
    assert _find_balanced_end('{"incomplete": ', 0) == -1


# ── _strip_json_echo ──────────────────────────────


def test_strip_json_echo_removes_law_search_dump():
    text = (
        "먼저 검색하겠습니다.\n\n"
        '[{"법령명_한글":"개인정보 보호법","MST":"4868","source_url":"https://x"}]'
        "\n\n가장 최신 법령으로 제15조를 조회합니다."
    )
    out = _strip_json_echo(text)
    assert "법령명_한글" not in out
    assert "먼저 검색하겠습니다" in out
    assert "제15조를 조회합니다" in out


def test_strip_json_echo_removes_single_object_dump():
    text = (
        "결과:\n"
        '{"mst":"4868","article":{"title":"X","text":"..."},"source_url":"https://y"}\n'
        "답변을 작성합니다."
    )
    out = _strip_json_echo(text)
    assert '"mst"' not in out
    assert "답변을 작성합니다" in out


def test_strip_json_echo_preserves_plain_text_braces():
    """Braces in normal prose (no JSON keys) must survive."""
    text = "이 사건에서 피고는 {피고}라는 가명을 사용했습니다."
    out = _strip_json_echo(text)
    assert out == text


def test_strip_json_echo_preserves_inline_code():
    text = "코드 예시: `json.dumps({\"key\": \"value\"})` 가 기본입니다."
    out = _strip_json_echo(text)
    # Inline braces without legal keys are kept verbatim.
    assert "json.dumps" in out


def test_strip_json_echo_collapses_blank_lines():
    text = '앞\n\n\n\n[{"법령명":"A","MST":"1"}]\n\n\n\n뒤'
    out = _strip_json_echo(text)
    assert "법령명" not in out
    assert "\n\n\n" not in out


# ── _sanitise_urls ────────────────────────────────


def test_sanitise_urls_strips_revision_block():
    url = "https://www.law.go.kr/법령/개인정보보호법/(20250918,497716,20250916)/제15조"
    out = _sanitise_urls(url)
    assert "(" not in out
    assert out == "https://www.law.go.kr/법령/개인정보보호법/제15조"


def test_sanitise_urls_ignores_clean_urls():
    url = "https://www.law.go.kr/법령/개인정보보호법"
    assert _sanitise_urls(url) == url


def test_sanitise_urls_handles_multiple_in_one_text():
    text = (
        "링크 1: https://www.law.go.kr/법령/A/(20240101,100,20231231)/제1조\n"
        "링크 2: https://www.law.go.kr/법령/B/(20250101,200,20241231)"
    )
    out = _sanitise_urls(text)
    assert "(" not in out
    assert "A/제1조" in out
    assert "B" in out


# ── _strip_toolu_markers ──────────────────────────


def test_strip_toolu_markers_removes_ids():
    text = "toolu_01NqwtWVxJe8Bs3yjLQW5Ybm\n  law_search"
    out = _strip_toolu_markers(text)
    assert "toolu_" not in out
    assert "law_search" in out


def test_strip_toolu_markers_noop_on_clean_text():
    text = "개인정보 보호법 제15조"
    assert _strip_toolu_markers(text) == text


# ── _strip_tool_result_labels ─────────────────────


def test_strip_tool_result_labels_removes_law_search_result():
    text = "[Law Search Result]\n\n[Law Article Result]\n\n답변 내용입니다."
    out = _strip_tool_result_labels(text)
    assert "[Law Search Result]" not in out
    assert "[Law Article Result]" not in out
    assert "답변 내용입니다" in out


def test_strip_tool_result_labels_removes_tool_result_label():
    text = "[Tool Result: law_search]\n답변."
    out = _strip_tool_result_labels(text)
    assert "Tool Result" not in out
    assert "답변" in out


def test_strip_tool_result_labels_removes_lowercase_variants():
    text = "[law_get_article result]\n[law search result]\n실제 답변."
    out = _strip_tool_result_labels(text)
    assert "law_get_article" not in out
    assert "law search result" not in out
    assert "실제 답변" in out


def test_strip_tool_result_labels_preserves_legitimate_brackets():
    """Regular markdown links and article citations must not be eaten."""
    text = (
        "개인정보 보호법 제15조는 [인용]으로 표시합니다.\n"
        "[법령 링크](https://www.law.go.kr/법령/개인정보보호법)"
    )
    out = _strip_tool_result_labels(text)
    assert "[인용]" in out
    assert "[법령 링크]" in out


# ── _parse_cli_response ───────────────────────────


def test_parse_cli_response_plain_text_passthrough():
    text = "먼저 개인정보보호법을 검색하겠습니다."
    cleaned, calls = _parse_cli_response(text)
    assert cleaned == text
    assert calls == []


def test_parse_cli_response_extracts_native_tool_use():
    """Simulate CLI fallback where text blocks were empty and raw NDJSON leaked.

    Each line is one stream-json event: an assistant message containing a
    single tool_use content block. Our parser should promote it to a
    native tool_call dict.
    """
    ndjson = "\n".join([
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_01NqwtWVxJe8Bs3yjLQW5Ybm","name":"law_search","input":{"query":"개인정보보호법"}}]}}',
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_01E6KBNsPdnbqCqFLXfH3tgA","name":"law_get_article","input":{"mst":"497716","jo":"제15조"}}]}}',
        '{"type":"result","subtype":"success","result":""}',
    ])
    text, calls = _parse_cli_response(ndjson)
    assert text == ""  # no text blocks in input
    assert len(calls) == 2
    assert calls[0]["name"] == "law_search"
    assert calls[0]["input"]["query"] == "개인정보보호법"
    assert calls[1]["name"] == "law_get_article"
    assert calls[1]["input"]["mst"] == "497716"
    assert calls[1]["input"]["jo"] == "제15조"


def test_parse_cli_response_mixed_text_and_tool_use():
    ndjson = "\n".join([
        '{"type":"assistant","message":{"content":[{"type":"text","text":"먼저 검색합니다."},{"type":"tool_use","id":"toolu_xyz","name":"law_search","input":{"query":"X"}}]}}',
    ])
    text, calls = _parse_cli_response(ndjson)
    assert "먼저 검색합니다" in text
    assert len(calls) == 1
    assert calls[0]["name"] == "law_search"


def test_parse_cli_response_handles_malformed_ndjson():
    """Mid-stream parse errors should not crash; parser should return
    whatever it successfully extracted."""
    ndjson = "\n".join([
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"law_search","input":{"query":"A"}}]}}',
        'NOT_JSON',
        '{"broken"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"law_get_article","input":{"mst":"1","jo":"제1조"}}]}}',
    ])
    text, calls = _parse_cli_response(ndjson)
    assert len(calls) == 2
    assert calls[0]["name"] == "law_search"
    assert calls[1]["name"] == "law_get_article"


def test_parse_cli_response_empty_string():
    text, calls = _parse_cli_response("")
    assert text == ""
    assert calls == []
