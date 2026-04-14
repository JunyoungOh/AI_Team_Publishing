"""Server-side professional report renderer.

A single Python module that renders every mode's final report (instant,
my-strategy, overtime, dev, upgrade) using one neutral, document-style
template. CLI sessions only need to write a structured `report.json`; this
module turns that into the `results.html` the user actually sees.

Why a server-side template?
- The CLI used to inline its own CSS (gradients, neon yellow accents) in
  every prompt — results were inconsistent and tilted toward "marketing
  brochure" instead of "consulting document".
- When a CLI stream gets cut by an idle timeout, we still need a meaningful
  HTML to render. A renderer that owns the template means partial-result
  fallbacks share the exact same look as the happy-path output.
"""

from __future__ import annotations

import html as _html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

# Reuse markdown helpers from the legacy exporter so we don't duplicate the
# lazy-import dance.
from src.utils.report_exporter import _md_to_html, _sanitize_report_html


# ── Public types ──────────────────────────────────────────


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return _html.escape(str(value))


# ── CSS (single source of truth) ──────────────────────────


_PROFESSIONAL_CSS = """
:root {
  --bg: #f3f4f6;
  --surface: #ffffff;
  --surface-alt: #f7f8fa;
  --border: #e4e7ec;
  --border-strong: #d0d5dd;
  --text: #101828;
  --text-muted: #475467;
  --text-subtle: #667085;
  --accent: #1f4e8c;
  --accent-soft: #eef3fb;
  --warning: #b54708;
  --warning-soft: #fef3c7;
  --warning-border: #f0c674;
  --info: #1f4e8c;
  --info-soft: #eef3fb;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', 'Apple SD Gothic Neo', 'Noto Sans KR',
               -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 14px;
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.doc {
  max-width: 860px;
  margin: 0 auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-top: none;
  min-height: 100vh;
}

.doc-header {
  padding: 48px 56px 28px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

.doc-header .mode-label {
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 4px 10px;
  border-radius: 3px;
  margin-bottom: 16px;
}

.doc-header h1 {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.35;
  margin: 0 0 12px;
  color: var(--text);
  letter-spacing: -0.01em;
}

.doc-header .meta {
  font-size: 12px;
  color: var(--text-subtle);
  margin: 0;
}

.doc-header .meta span + span::before {
  content: " · ";
  margin: 0 4px;
  color: var(--border-strong);
}

.doc-body {
  padding: 32px 56px 56px;
}

.doc-body h2 {
  font-size: 18px;
  font-weight: 700;
  margin: 36px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  letter-spacing: -0.005em;
}

.doc-body h2:first-child { margin-top: 0; }

.doc-body h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 24px 0 8px;
  color: var(--text);
}

.doc-body p {
  margin: 0 0 14px;
  color: var(--text);
}

.doc-body ul, .doc-body ol {
  margin: 0 0 16px;
  padding-left: 22px;
  color: var(--text);
}

.doc-body li { margin: 4px 0; }

.doc-body a {
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px solid transparent;
}

.doc-body a:hover { border-bottom-color: var(--accent); }

.doc-body code {
  font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
  font-size: 12.5px;
  background: var(--surface-alt);
  border: 1px solid var(--border);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--text);
}

.doc-body pre {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 14px 16px;
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.55;
  margin: 0 0 16px;
}

.doc-body pre code {
  background: none;
  border: none;
  padding: 0;
}

.doc-body blockquote {
  margin: 0 0 16px;
  padding: 4px 0 4px 16px;
  border-left: 3px solid var(--border-strong);
  color: var(--text-muted);
}

.doc-body table {
  width: 100%;
  border-collapse: collapse;
  margin: 8px 0 20px;
  font-size: 13px;
}

.doc-body th, .doc-body td {
  text-align: left;
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}

.doc-body th {
  font-weight: 600;
  color: var(--text-muted);
  border-bottom: 1.5px solid var(--border-strong);
  background: var(--surface-alt);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.doc-body tr:last-child td { border-bottom: none; }

.summary-block {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  padding: 18px 22px;
  margin: 0 0 28px;
  border-radius: 0 4px 4px 0;
}

.summary-block h2 {
  margin-top: 0;
  border-bottom: none;
  padding-bottom: 0;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--accent);
  margin-bottom: 8px;
}

.summary-block p:last-child,
.summary-block ul:last-child,
.summary-block ol:last-child { margin-bottom: 0; }

.recommendations {
  list-style: none;
  padding: 0;
  margin: 0 0 16px;
  counter-reset: rec;
}

.recommendations li {
  counter-increment: rec;
  padding: 10px 0 10px 36px;
  border-bottom: 1px solid var(--border);
  position: relative;
  color: var(--text);
}

.recommendations li:last-child { border-bottom: none; }

.recommendations li::before {
  content: counter(rec, decimal-leading-zero);
  position: absolute;
  left: 0;
  top: 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
  font-feature-settings: "tnum";
}

.sources {
  font-size: 12.5px;
  color: var(--text-muted);
  margin: 0;
  padding-left: 20px;
}

.sources li { margin: 3px 0; word-break: break-all; }

.banner {
  border: 1px solid var(--border);
  border-left: 4px solid var(--info);
  background: var(--info-soft);
  padding: 16px 20px;
  margin: 0 0 28px;
  border-radius: 0 4px 4px 0;
  font-size: 13px;
  color: var(--text);
}

.banner.banner-warning {
  border-left-color: var(--warning);
  background: var(--warning-soft);
  border-color: var(--warning-border);
  color: #5c2c00;
}

.banner-title {
  font-weight: 700;
  font-size: 14px;
  margin: 0 0 6px;
  color: inherit;
}

.banner p { margin: 4px 0; color: inherit; }
.banner ul { margin: 8px 0 0; padding-left: 20px; color: inherit; }
.banner li { margin: 2px 0; }

.fallback-content {
  background: var(--surface-alt);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 20px 24px;
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text);
  word-wrap: break-word;
}

.doc-footer {
  padding: 20px 56px 32px;
  border-top: 1px solid var(--border);
  font-size: 11.5px;
  color: var(--text-subtle);
  text-align: center;
  background: var(--surface);
}

@media (max-width: 768px) {
  .doc-header { padding: 32px 24px 20px; }
  .doc-body { padding: 24px; }
  .doc-footer { padding: 16px 24px 24px; }
  .doc-header h1 { font-size: 22px; }
}

@media print {
  @page { size: A4; margin: 18mm; }
  html, body { background: #fff !important; font-size: 11pt; }
  .doc {
    max-width: none;
    border: none;
    margin: 0;
    background: #fff;
  }
  .doc-header,
  .doc-body,
  .doc-footer { padding-left: 0; padding-right: 0; background: #fff; }
  .doc-body h2 { page-break-after: avoid; }
  .doc-body table, .doc-body pre, .summary-block { page-break-inside: avoid; }
  .banner { background: #fff; }
  a { color: var(--accent); text-decoration: none; }
}
""".strip()


# ── Internal helpers ──────────────────────────────────────


def _render_table(table: dict | None) -> str:
    if not isinstance(table, dict):
        return ""
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if not headers and not rows:
        return ""

    head_html = ""
    if headers:
        cells = "".join(f"<th>{_esc(h)}</th>" for h in headers)
        head_html = f"<thead><tr>{cells}</tr></thead>"

    body_rows = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        cells = "".join(f"<td>{_esc(c)}</td>" for c in row)
        body_rows.append(f"<tr>{cells}</tr>")
    body_html = f"<tbody>{''.join(body_rows)}</tbody>" if body_rows else ""

    return f"<table>{head_html}{body_html}</table>"


def _render_section(section: dict) -> str:
    """Render one section dict to HTML."""
    if not isinstance(section, dict):
        return ""

    parts: list[str] = []
    heading = section.get("heading") or section.get("title")
    if heading:
        parts.append(f"<h2>{_esc(heading)}</h2>")

    body_md = section.get("body_md") or section.get("body") or ""
    if body_md:
        parts.append(_md_to_html(_sanitize_report_html(str(body_md))))

    table_html = _render_table(section.get("table"))
    if table_html:
        parts.append(table_html)

    sources = section.get("sources") or []
    if isinstance(sources, list) and sources:
        items = "".join(f"<li>{_esc(s)}</li>" for s in sources)
        parts.append(f'<ul class="sources">{items}</ul>')

    return "".join(parts)


def _render_summary_block(executive_summary: str | None) -> str:
    if not executive_summary:
        return ""
    body_html = _md_to_html(_sanitize_report_html(str(executive_summary)))
    return (
        '<section class="summary-block">'
        '<h2>Executive Summary</h2>'
        f"{body_html}"
        "</section>"
    )


def _render_recommendations(items: list[str] | None) -> str:
    if not items:
        return ""
    safe_items = [str(i).strip() for i in items if str(i).strip()]
    if not safe_items:
        return ""
    lis = "".join(f"<li>{_md_to_html(i).removeprefix('<p>').removesuffix('</p>')}</li>" for i in safe_items)
    return f'<h2>Recommendations</h2><ol class="recommendations">{lis}</ol>'


def _render_global_sources(items: list[str] | None) -> str:
    if not items:
        return ""
    safe = [str(s).strip() for s in items if str(s).strip()]
    if not safe:
        return ""
    lis = "".join(f"<li>{_esc(s)}</li>" for s in safe)
    return f'<h2>Sources</h2><ul class="sources">{lis}</ul>'


def _render_banner(banner: dict | None) -> str:
    if not banner:
        return ""
    level = (banner.get("level") or "info").lower()
    css_class = "banner banner-warning" if level == "warning" else "banner"
    title = banner.get("title", "")
    body = banner.get("body", "")
    bullets = banner.get("bullets") or []

    parts = [f'<div class="{css_class}">']
    if title:
        parts.append(f'<div class="banner-title">{_esc(title)}</div>')
    if body:
        parts.append(f"<p>{_esc(body)}</p>")
    if bullets:
        items = "".join(f"<li>{_esc(b)}</li>" for b in bullets)
        parts.append(f"<ul>{items}</ul>")
    parts.append("</div>")
    return "".join(parts)


def _document_shell(
    *,
    title: str,
    mode_label: str,
    generated_at: str,
    session_id: str,
    body_html: str,
) -> str:
    return (
        '<!DOCTYPE html>\n'
        '<html lang="ko">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_PROFESSIONAL_CSS}</style>\n"
        "</head>\n"
        '<body>\n'
        '<article class="doc">\n'
        '<header class="doc-header">\n'
        f'<span class="mode-label">{_esc(mode_label)}</span>\n'
        f"<h1>{_esc(title)}</h1>\n"
        '<p class="meta">'
        f"<span>Generated {_esc(generated_at)}</span>"
        f"<span>Session {_esc(session_id)}</span>"
        "</p>\n"
        "</header>\n"
        f'<section class="doc-body">{body_html}</section>\n'
        '<footer class="doc-footer">'
        f"Generated by Enterprise Agent System · {_esc(generated_at)}"
        "</footer>\n"
        "</article>\n"
        "</body>\n"
        "</html>\n"
    )


# ── Public API ────────────────────────────────────────────


def render_report(
    *,
    title: str,
    sections: list[dict] | None = None,
    executive_summary: str | None = None,
    recommendations: list[str] | None = None,
    sources: list[str] | None = None,
    banner: dict | None = None,
    mode_label: str = "Analysis Report",
    generated_at: str | None = None,
    session_id: str = "",
) -> str:
    """Render a structured report payload to a self-contained HTML document."""
    if not generated_at:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    body_parts: list[str] = []
    body_parts.append(_render_banner(banner))
    body_parts.append(_render_summary_block(executive_summary))

    for section in sections or []:
        body_parts.append(_render_section(section))

    body_parts.append(_render_recommendations(recommendations))
    body_parts.append(_render_global_sources(sources))

    body_html = "".join(p for p in body_parts if p)
    if not body_html.strip():
        body_html = (
            '<p style="color:var(--text-muted)">'
            "보고서 내용이 비어 있습니다."
            "</p>"
        )

    return _document_shell(
        title=title or "Report",
        mode_label=mode_label,
        generated_at=generated_at,
        session_id=session_id or "—",
        body_html=body_html,
    )


def render_from_json_file(
    path: str | Path,
    *,
    banner: dict | None = None,
    mode_label: str = "Analysis Report",
    session_id: str = "",
    fallback_title: str = "Report",
) -> str:
    """Read a `report.json` written by the CLI and render it.

    Tolerates missing fields — only `title` or any one of the content
    fields needs to be present.
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"report.json root must be an object, got {type(data).__name__}")

    return render_report(
        title=data.get("title") or fallback_title,
        executive_summary=data.get("executive_summary") or data.get("summary"),
        sections=data.get("sections") or [],
        recommendations=data.get("recommendations") or [],
        sources=data.get("sources") or [],
        banner=banner,
        mode_label=mode_label,
        generated_at=data.get("generated_at"),
        session_id=session_id,
    )


def render_from_markdown_file(
    path: str | Path,
    *,
    title: str,
    banner: dict | None = None,
    mode_label: str = "Analysis Report",
    session_id: str = "",
) -> str:
    """Render a single markdown file inside the professional shell.

    Used as a fallback when the CLI produced markdown but no JSON.
    """
    p = Path(path)
    md_text = p.read_text(encoding="utf-8")
    sections = [{"heading": None, "body_md": md_text}]
    return render_report(
        title=title,
        sections=sections,
        banner=banner,
        mode_label=mode_label,
        session_id=session_id,
    )


_FALLBACK_MESSAGES = {
    "stream_idle_timeout": {
        "title": "AI 응답 스트림이 중간에 끊겼습니다",
        "body": (
            "모델이 최종 결과를 정리하기 전에 응답 스트림이 일정 시간 동안 "
            "조용해져 자동으로 종료되었습니다. 아래는 작업 도중 모델이 생성한 "
            "부분 결과입니다."
        ),
        "bullets": [
            "다시 실행하면 대부분 정상적으로 끝납니다",
            "작업 범위를 좁히거나 분석 깊이를 'standard'로 낮추면 안정성이 올라갑니다",
        ],
    },
    "timeout": {
        "title": "작업이 시간 제한으로 중단되었습니다",
        "body": (
            "AI가 최종 보고서를 완성하기 전에 타임아웃이 발생했습니다. "
            "아래는 작업 중 생성된 부분 결과입니다."
        ),
        "bullets": [
            "작업 범위를 더 구체적으로 좁혀보세요",
            "분석 깊이(depth)를 light 또는 standard로 변경",
            "관점(perspectives) 개수 줄이기",
        ],
    },
    "empty_result": {
        "title": "AI 응답이 비어 있습니다",
        "body": "CLI 세션이 결과를 반환하지 않았습니다. 잠시 후 다시 시도해보세요.",
        "bullets": [],
    },
    "no_artifact": {
        "title": "최종 보고서 파일이 생성되지 않았습니다",
        "body": (
            "AI가 작업을 끝냈지만 결과 파일을 저장하지 못했습니다. "
            "아래는 모델이 마지막으로 출력한 텍스트입니다."
        ),
        "bullets": ["다시 실행해보세요"],
    },
    "pipeline_error": {
        "title": "파이프라인 실행 중 오류가 발생했습니다",
        "body": "내부 처리 단계에서 예외가 발생하여 작업이 중단되었습니다.",
        "bullets": [],
    },
    "finalize_failed": {
        "title": "보고서 마무리 단계에서 실패했습니다",
        "body": (
            "부분 결과를 가지고 보고서를 다시 만들려 했으나 실패했습니다. "
            "아래는 모델이 작업 중 출력한 텍스트입니다."
        ),
        "bullets": [],
    },
}


def render_partial_fallback(
    *,
    user_task: str,
    session_id: str,
    raw_text: str,
    reason: str,
    timeout_s: int | None = None,
    mode_label: str = "Partial Result",
    extra_detail: str | None = None,
) -> str:
    """Render a partial-result HTML when normal rendering paths failed.

    Uses the same professional shell so the user does not perceive a
    "crash" — they see a calm warning banner above whatever raw text the
    model managed to produce.
    """
    spec = _FALLBACK_MESSAGES.get(reason, _FALLBACK_MESSAGES["no_artifact"])
    title_text = spec["title"]
    body_text = spec["body"]
    if timeout_s and reason in ("stream_idle_timeout", "timeout"):
        body_text = body_text + f" (제한 시간 {timeout_s}초)"
    if extra_detail:
        body_text = body_text + " " + extra_detail

    banner = {
        "level": "warning",
        "title": title_text,
        "body": body_text,
        "bullets": spec["bullets"],
    }

    raw_clip = (raw_text or "(결과 없음)")[:60000]
    fallback_section = {
        "heading": "작업 중 출력된 내용",
        "body_md": f'<div class="fallback-content">{_esc(raw_clip)}</div>',
    }

    return render_report(
        title=user_task or "Partial Result",
        sections=[fallback_section],
        banner=banner,
        mode_label=mode_label,
        session_id=session_id,
    )


# ── Completeness check ────────────────────────────────────


def is_complete_html(path: str | Path, *, min_size: int = 4000) -> bool:
    """Return True if the file looks like a finished HTML document.

    Used to decide whether a CLI-written `results.html` is good enough
    to keep, or should be replaced with the renderer output.
    """
    p = Path(path)
    if not p.exists():
        return False
    try:
        size = p.stat().st_size
        if size < min_size:
            return False
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if "</html>" not in text.lower():
        return False
    if "<h1" not in text.lower() and "<h2" not in text.lower():
        return False
    return True
