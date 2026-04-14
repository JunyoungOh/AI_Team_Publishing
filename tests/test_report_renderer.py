"""Unit tests for src.utils.report_renderer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.report_renderer import (
    is_complete_html,
    render_from_json_file,
    render_from_markdown_file,
    render_partial_fallback,
    render_report,
)


def _required_shell_tags(html: str) -> None:
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert 'class="doc"' in html
    assert 'class="doc-header"' in html
    assert 'class="doc-body"' in html
    assert 'class="doc-footer"' in html


def test_render_report_minimal_has_required_structure():
    html = render_report(
        title="카카오 AI 인재 분석",
        executive_summary="핵심 요약 한 문단.",
        sections=[
            {"heading": "배경", "body_md": "**배경** 설명입니다."},
        ],
        recommendations=["A안 채택", "B안 보류"],
        sources=["https://example.com/1"],
        mode_label="Analysis Report",
    )
    _required_shell_tags(html)
    assert "카카오 AI 인재 분석" in html
    assert "Analysis Report" in html
    assert "Executive Summary" in html
    assert "배경" in html
    assert "Recommendations" in html
    assert "Sources" in html
    assert "https://example.com/1" in html


def test_render_report_table_renders_thead_and_tbody():
    html = render_report(
        title="t",
        sections=[
            {
                "heading": "시장 점유율",
                "body_md": "표에 정리.",
                "table": {
                    "headers": ["회사", "점유율"],
                    "rows": [["A", "30%"], ["B", "25%"]],
                },
            }
        ],
    )
    assert "<thead>" in html and "<tbody>" in html
    assert "<th>회사</th>" in html
    assert "<td>A</td>" in html


def test_render_report_empty_body_shows_placeholder():
    html = render_report(title="empty")
    assert "보고서 내용이 비어 있습니다" in html


def test_render_partial_fallback_warning_banner_for_stream_idle():
    html = render_partial_fallback(
        user_task="카카오 분석",
        session_id="abc123",
        raw_text="중간까지 모은 내용",
        reason="stream_idle_timeout",
        timeout_s=900,
    )
    _required_shell_tags(html)
    assert "banner-warning" in html
    assert "스트림" in html or "조용해져" in html
    assert "중간까지 모은 내용" in html
    assert "900" in html


def test_render_partial_fallback_unknown_reason_uses_no_artifact():
    html = render_partial_fallback(
        user_task="t",
        session_id="s",
        raw_text="x",
        reason="totally_unknown_reason",
    )
    assert "banner-warning" in html
    assert "최종 보고서 파일이 생성되지 않았습니다" in html


def test_render_from_json_file_tolerates_missing_optional_fields(tmp_path: Path):
    payload = {
        "title": "minimal",
        "sections": [{"heading": "Only heading"}],
    }
    p = tmp_path / "report.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    html = render_from_json_file(p, mode_label="Test Report")
    _required_shell_tags(html)
    assert "minimal" in html
    assert "Only heading" in html
    assert "Test Report" in html
    assert "Recommendations" not in html
    assert "Sources" not in html


def test_render_from_json_file_full_payload(tmp_path: Path):
    payload = {
        "title": "Full Report",
        "executive_summary": "Summary text",
        "sections": [
            {"heading": "Section 1", "body_md": "Body 1", "sources": ["src1"]},
            {"heading": "Section 2", "body_md": "Body 2"},
        ],
        "recommendations": ["Do X"],
        "sources": ["https://a", "https://b"],
    }
    p = tmp_path / "report.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    html = render_from_json_file(p)
    assert "Full Report" in html
    assert "Section 1" in html and "Section 2" in html
    assert "Do X" in html
    assert "https://a" in html and "https://b" in html


def test_render_from_markdown_file(tmp_path: Path):
    md = "# Heading\n\nSome **bold** text.\n\n- item 1\n- item 2\n"
    p = tmp_path / "report.md"
    p.write_text(md, encoding="utf-8")
    html = render_from_markdown_file(p, title="MD Title")
    _required_shell_tags(html)
    assert "MD Title" in html
    assert "item 1" in html


def test_is_complete_html_rejects_truncated(tmp_path: Path):
    p = tmp_path / "results.html"
    p.write_text("<p>x</p>", encoding="utf-8")
    assert is_complete_html(p) is False


def test_is_complete_html_rejects_missing_closing_tag(tmp_path: Path):
    p = tmp_path / "results.html"
    body = "<html><body>" + ("<h1>title</h1><p>x</p>" * 200)
    p.write_text(body, encoding="utf-8")
    assert is_complete_html(p) is False


def test_is_complete_html_accepts_finished_document(tmp_path: Path):
    p = tmp_path / "results.html"
    body = (
        "<!DOCTYPE html><html><head><title>t</title></head><body>"
        + "<h1>Title</h1>"
        + "<p>filler text. </p>" * 300
        + "</body></html>"
    )
    p.write_text(body, encoding="utf-8")
    assert is_complete_html(p) is True


def test_is_complete_html_handles_missing_file(tmp_path: Path):
    assert is_complete_html(tmp_path / "nope.html") is False
