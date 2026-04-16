"""Report node — LLM autonomously writes a complete self-contained HTML report.

Playbook-style: the LLM receives the full transcript and uses the Write tool
to create a single self-contained HTML document at a specified path. Python
only prepares the directory, hands the LLM the target path, and reads the
file back for WebSocket streaming and history-DB backfill.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from src.config.settings import get_settings
from src.discussion.prompts.moderator import STYLE_DESCRIPTIONS
from src.discussion.prompts.report import REPORT_SYSTEM
from src.discussion.state import DiscussionState
from src.utils.bridge_factory import get_bridge

logger = logging.getLogger(__name__)


def _format_participants(config) -> str:
    """AI 참가자 + 사람 참가자(참여형 모드)를 모두 나열.

    사람 참가자를 빼면 transcript에는 등장하는데 참가자 목록엔 없어
    LLM이 reconcile을 시도하느라 thinking 토큰을 소진하거나 generation이
    꼬이는 현상이 발생했음.
    """
    lines = [f"- {p.name}: {p.persona}" for p in config.participants]
    human = getattr(config, "human_participant", None)
    if human:
        persona = (human.persona or "실제 사람 참가자").strip()
        lines.append(f"- {human.name} (사람): {persona}")
    return "\n".join(lines)


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


def _prepare_output_dir(session_id: str) -> Path | None:
    """Create the report output directory. Returns absolute path or None."""
    try:
        settings = get_settings()
        if not settings.report_export_enabled:
            return None
        base_dir = Path(settings.report_output_dir).resolve()
        folder_name = (
            f"disc_{session_id}"
            if session_id
            else f"disc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        output_dir = base_dir / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir
    except Exception:
        logger.warning("discussion_output_dir_failed", exc_info=True)
        return None


def _write_metadata(output_dir: Path, config, session_id: str) -> None:
    """Sidecar metadata.json — used by the history-viewer API to backfill
    the discussion_reports DB row if the WebSocket session was torn down
    before emitting disc_report (e.g., browser closed mid-closing).
    """
    try:
        meta = {
            "session_id": session_id,
            "topic": config.topic,
            "participants": [p.name for p in config.participants],
            "style": config.style,
            "created_at": datetime.now().isoformat(),
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8",
        )
    except Exception:
        logger.warning("discussion_metadata_save_failed", exc_info=True)


def _fallback_html(config, utterances: list[dict]) -> str:
    """Minimal self-contained HTML used when the LLM path fails.

    Kept simple on purpose: the goal is to preserve the transcript, not to
    replicate the rich playbook design. Three defensive layers above this
    make reaching the fallback unlikely.
    """
    safe_topic = (config.topic or "토론 리포트").replace("<", "&lt;").replace(">", "&gt;")
    participant_list = ", ".join(p.name for p in config.participants) or "—"
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    body_parts: list[str] = []
    current_round = -1
    for u in utterances:
        r = u.get("round", 0)
        if r != current_round:
            current_round = r
            label = "오프닝" if r == 0 else f"라운드 {r}"
            body_parts.append(f'<h2 style="margin-top:32px;color:#4a90d9">{label}</h2>')
        name = (u.get("speaker_name") or u.get("speaker_id") or "?").replace("<", "&lt;")
        content = (u.get("content") or "").replace("<", "&lt;").replace(">", "&gt;")
        body_parts.append(
            f'<div style="margin:12px 0;padding:12px 16px;border-left:3px solid #4a90d9;background:#f6f8fa;border-radius:4px">'
            f'<strong style="color:#0969da">{name}</strong>'
            f'<p style="margin:6px 0 0;white-space:pre-wrap">{content}</p>'
            f'</div>'
        )
    body_html = "\n".join(body_parts)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>토론 리포트 — {safe_topic}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         max-width: 860px; margin: 0 auto; padding: 32px 24px; background: #ffffff;
         color: #1f2328; line-height: 1.6; }}
  h1 {{ font-size: 1.6em; margin-bottom: 6px; }}
  .meta {{ color: #656d76; font-size: 0.85em; margin-bottom: 24px;
           border-bottom: 1px solid #d1d9e0; padding-bottom: 12px; }}
  .notice {{ padding: 12px 16px; margin-bottom: 24px; background: #fff8e1;
             border-left: 3px solid #d4a72c; border-radius: 4px; color: #9a6700; }}
  @media print {{ body {{ background: #fff; }} }}
</style>
</head>
<body>
<h1>{safe_topic}</h1>
<div class="meta">{participant_list} · {generated}</div>
<div class="notice">리포트 자동 생성에 실패하여 원본 토론 기록을 표시합니다.</div>
{body_html}
</body>
</html>
"""


async def discussion_report(state: DiscussionState, config: dict | None = None) -> dict:
    """Generate final discussion report as a complete self-contained HTML file.

    Flow:
    1. Emit phase=report immediately so the UI can start its live timer
       instead of staying stuck on the previous "closing" message.
    2. Prepare output directory and absolute target path
    3. Ask the LLM to Write the complete HTML file directly to that path,
       with an on_event hook that emits a stage update when Write fires.
    4. Read the file back → stream payload + history DB backfill
    5. Three-layer defense: file exists? → raw response is HTML? → transcript fallback
    """
    disc_config = state["config"]
    session_id = state.get("session_id", "")

    # Direct access to the DiscussionSession — the pipeline runner stashes
    # it in graph_config["configurable"]["session"] (see session.py:205).
    # We use it to push real-time progress events that the default engine
    # flow can't emit (nodes return a single dict at the end; no mid-node
    # yields). This is the recommended pattern for long-running nodes that
    # need streaming feedback.
    session = None
    if config:
        session = (config.get("configurable") or {}).get("session")

    async def _emit_stage(message: str) -> None:
        if session is None:
            return
        try:
            await session._send({
                "type": "disc_report_stage",
                "data": {"message": message},
            })
        except Exception:
            logger.debug("disc_report_stage_emit_failed", exc_info=True)

    async def _emit_phase_report() -> None:
        if session is None:
            return
        try:
            await session._send({
                "type": "disc_phase",
                "data": {"phase": "report", "node": "report"},
            })
        except Exception:
            logger.debug("disc_phase_report_emit_failed", exc_info=True)

    # Announce the transition from closing → report BEFORE the slow LLM call
    # so the UI can start its elapsed-time counter immediately.
    await _emit_phase_report()

    output_dir = _prepare_output_dir(session_id)

    if output_dir is None:
        # Report export disabled or directory prep failed — return in-memory fallback
        html = _fallback_html(disc_config, state["utterances"])
        return {
            "final_report_html": html,
            "report_file_path": "",
            "phase": "done",
        }

    report_path = output_dir / "report.html"
    bridge = get_bridge()

    prompt = REPORT_SYSTEM.format(
        topic=disc_config.topic,
        style=STYLE_DESCRIPTIONS.get(disc_config.style, disc_config.style),
        participants_info=_format_participants(disc_config),
        full_transcript=_format_full_transcript(state["utterances"]),
        output_path=str(report_path),
    )

    def _on_cli_event(ev: dict):
        """Forward Claude CLI tool-use events to the discussion WebSocket.

        The CLI emits a tool_use event the moment the LLM calls Write.
        That is the real "writing" signal — not a fake progress bar.
        """
        if ev.get("action") != "tool_use":
            return None
        if ev.get("tool") == "Write":
            return _emit_stage("\u270D\uFE0F HTML \uD30C\uC77C \uC791\uC131 \uC911...")
        return None

    llm_response_text = ""
    try:
        # 간소화된 프롬프트 + effort="medium"(요약·인사이트 추출에 필수) +
        # 타임아웃 10분. effort=low는 요약·인사이트 품질이 떨어져서 medium 유지.
        # 10분 내 완성 안 되면 fallback HTML로 전환 (20분 대기 근본 차단).
        llm_response_text = await bridge.raw_query(
            system_prompt=prompt,
            user_message=(
                f"토론 주제 '{disc_config.topic}'의 최종 리포트를 "
                f"'{report_path}' 경로에 Write 툴로 저장하세요. "
                f"간결한 HTML 한 파일이어야 합니다."
            ),
            model=disc_config.model_moderator,
            allowed_tools=["Write"],
            extra_dirs=[str(output_dir)],
            timeout=600,
            max_turns=3,
            effort="medium",
            on_event=_on_cli_event,
        )
    except Exception as e:
        logger.warning("discussion_report_llm_failed: %s", e)
    finally:
        await bridge.close()

    # Defense layer 1: LLM wrote the file directly — read it back
    final_html = ""
    if report_path.exists():
        try:
            final_html = report_path.read_text(encoding="utf-8").strip()
        except Exception:
            logger.warning("discussion_report_read_failed", exc_info=True)

    # Defense layer 2: LLM returned HTML text but skipped Write — salvage it
    if not final_html and llm_response_text:
        stripped = llm_response_text.strip()
        if "<!doctype" in stripped.lower() or "<html" in stripped.lower():
            try:
                report_path.write_text(stripped, encoding="utf-8")
                final_html = stripped
                logger.info("discussion_report_salvaged_from_text")
            except Exception:
                logger.warning("discussion_report_salvage_write_failed", exc_info=True)

    # Defense layer 3: transcript fallback
    if not final_html:
        logger.warning("discussion_report_falling_back_to_transcript")
        final_html = _fallback_html(disc_config, state["utterances"])
        try:
            report_path.write_text(final_html, encoding="utf-8")
        except Exception:
            logger.warning("discussion_report_fallback_write_failed", exc_info=True)

    _write_metadata(output_dir, disc_config, session_id)
    logger.info("discussion_report_saved", extra={"path": str(report_path)})

    return {
        "final_report_html": final_html,
        "report_file_path": str(report_path),
        "phase": "done",
    }
