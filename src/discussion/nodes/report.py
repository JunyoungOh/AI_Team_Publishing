"""Report node — generates final HTML report from full transcript and saves to disk."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from src.config.settings import get_settings
from src.discussion.prompts.moderator import STYLE_DESCRIPTIONS
from src.discussion.prompts.report import REPORT_SYSTEM
from src.discussion.state import DiscussionState
from src.utils.bridge_factory import get_bridge

logger = logging.getLogger(__name__)


_FENCE_ONLY_LINE = re.compile(r"^\s*`{2,}\s*[a-zA-Z0-9]*\s*$")
_HTML_CLOSING_TAG = re.compile(r"</[a-zA-Z][^>]*>|<[a-zA-Z][^>]*/>")


def _sanitize_llm_html(raw: str) -> str:
    """Strip markdown code fences and trailing narration from LLM HTML output.

    LLMs occasionally wrap HTML in ```html ... ``` fences and append Korean
    commentary ("위 HTML 조각은 ...") despite explicit prompt instructions.
    These artifacts render as literal text on the final report and hurt the
    design. This function removes them defensively.
    """
    if not raw:
        return raw

    lines = raw.splitlines()
    lines = [ln for ln in lines if not _FENCE_ONLY_LINE.match(ln)]

    last_html_idx = -1
    for i, ln in enumerate(lines):
        if _HTML_CLOSING_TAG.search(ln):
            last_html_idx = i
    if last_html_idx != -1:
        lines = lines[: last_html_idx + 1]

    while lines and not lines[0].lstrip().startswith("<"):
        lines.pop(0)

    return "\n".join(lines).strip()


def _format_participants(config) -> str:
    return "\n".join(
        f"- {p.name}: {p.persona}"
        for p in config.participants
    )


def _format_full_transcript(utterances: list[dict]) -> str:
    lines = []
    current_round = -1
    for u in utterances:
        r = u.get("round", 0)
        if r != current_round:
            current_round = r
            label = "오프닝" if r == 0 else f"라운드 {r}"
            lines.append(f"\n── {label} ──")
        name = u.get("speaker_name", u.get("speaker_id", "?"))
        content = u.get("content", "")
        lines.append(f"[{name}] {content}")
    return "\n\n".join(lines)


_REPORT_WRAPPER = """\
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Discussion Report — {topic}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         max-width: 900px; margin: 0 auto; padding: 24px; background: #ffffff; color: #1f2328; line-height: 1.6; }}
  h1, h2, h3 {{ color: #1f2328; }}
  .meta {{ color: #656d76; font-size: 0.85em; margin-bottom: 24px; border-bottom: 1px solid #d1d9e0; padding-bottom: 12px; }}
  .disc-header {{ margin-bottom: 20px; }}
  .disc-section {{ margin-bottom: 24px; }}
  .disc-section-title {{ color: #0969da; border-bottom: 1px solid #d1d9e0; padding-bottom: 6px; }}
  .insight-card {{ border-left: 3px solid #d4a72c; padding: 12px 16px; margin: 8px 0; background: #fff8e1; border-radius: 4px; }}
  .participant-summary {{ border-left: 3px solid #0969da; padding: 12px 16px; margin: 8px 0; background: #f0f7ff; border-radius: 4px; }}
  .point-for {{ color: #1a7f37; }}
  .point-against {{ color: #cf222e; }}
  .consensus-box {{ border: 1px solid #1a7f37; padding: 12px 16px; margin: 8px 0; background: #f0fff4; border-radius: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8px 0; }}
  th, td {{ border: 1px solid #d1d9e0; padding: 8px 12px; text-align: left; }}
  th {{ background: #f6f8fa; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.75em; font-weight: 600; }}
  .tag-green {{ background: #dafbe1; color: #1a7f37; }}
  .tag-yellow {{ background: #fff8e1; color: #9a6700; }}
  .tag-red {{ background: #ffebe9; color: #cf222e; }}
  @media print {{ body {{ background: #fff; color: #000; }} h1,h2,h3 {{ color: #000; }} }}
</style>
</head>
<body>
<div class="meta">
  <strong>AI Discussion Report</strong> · {topic} · {participants} · {generated_at}
</div>
{content}
</body>
</html>
"""


def _save_report(html_fragment: str, config, session_id: str) -> str | None:
    """Save discussion report as self-contained HTML file.

    Returns the file path on success, None on failure. Never raises.
    """
    try:
        settings = get_settings()
        if not settings.report_export_enabled:
            return None

        base_dir = Path(settings.report_output_dir)
        folder_name = f"disc_{session_id}" if session_id else f"disc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        output_dir = base_dir / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)

        participant_names = ", ".join(p.name for p in config.participants)
        full_html = _REPORT_WRAPPER.format(
            topic=config.topic,
            participants=participant_names,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            content=html_fragment,
        )

        report_path = output_dir / "report.html"
        report_path.write_text(full_html, encoding="utf-8")

        # Sidecar metadata.json — used by the history-viewer API to backfill
        # the discussion_reports DB row if the report node finished but the
        # WebSocket session was already torn down (e.g., browser closed
        # mid-closing). Without this, an orphan report would sit on disk
        # invisible to the history viewer.
        try:
            import json as _json
            meta = {
                "session_id": session_id,
                "topic": config.topic,
                "participants": [p.name for p in config.participants],
                "style": config.style,
                "created_at": datetime.now().isoformat(),
            }
            (output_dir / "metadata.json").write_text(
                _json.dumps(meta, ensure_ascii=False), encoding="utf-8",
            )
        except Exception:
            logger.warning("discussion_metadata_save_failed", exc_info=True)

        logger.info("discussion_report_saved", path=str(report_path))
        return str(report_path)
    except Exception:
        logger.warning("discussion_report_save_failed", exc_info=True)
        return None


async def discussion_report(state: DiscussionState) -> dict:
    """Generate final discussion report as HTML."""
    config = state["config"]
    bridge = get_bridge()

    prompt = REPORT_SYSTEM.format(
        topic=config.topic,
        style=STYLE_DESCRIPTIONS.get(config.style, config.style),
        participants_info=_format_participants(config),
        full_transcript=_format_full_transcript(state["utterances"]),
    )

    try:
        html = await bridge.raw_query(
            system_prompt=prompt,
            user_message=f"토론 주제 '{config.topic}'의 최종 리포트를 HTML로 작성하세요.",
            model=config.model_moderator,
            allowed_tools=[],
            timeout=600,
            max_turns=2,
            effort="medium",
        )
        html_fragment = _sanitize_llm_html(html)
    except Exception as e:
        logger.warning("discussion_report_llm_failed: %s", e)
        # Fallback: render transcript as simple HTML
        lines = []
        for u in state["utterances"]:
            name = u.get("speaker_name", "?")
            content = u.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"<div class='participant-summary'><strong>{name}</strong><p>{content}</p></div>")
        html_fragment = (
            f"<h2>토론 기록 — {config.topic}</h2>"
            f"<p style='color:#f85149'>리포트 자동 생성에 실패하여 원본 토론 기록을 표시합니다.</p>"
            + "\n".join(lines)
        )
    finally:
        await bridge.close()
    session_id = state.get("session_id", "")
    saved_path = _save_report(html_fragment, config, session_id)

    return {
        "final_report_html": html_fragment,
        "report_file_path": saved_path or "",
        "phase": "done",
    }
