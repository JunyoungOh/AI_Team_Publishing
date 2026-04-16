"""FastAPI WebSocket server for Corporate Simulation UI.

Endpoints:
  GET  /              → Serve index.html (requires auth when membership enabled)
  WS   /ws            → AI Company mode (corporate simulation)
  WS   /ws/disc       → AI Discussion mode (multi-agent debate)
  WS   /ws/sec        → AI Secretary mode (fast chat assistant)
  WS   /ws/eng        → AI Engineering mode (code generation with phases)
  WS   /ws/datalab    → AI DataLab mode (data analysis with Zero-Retention)
  WS   /ws/foresight  → AI Foresight mode (trend analysis)
  WS   /ws/dandelion  → Dandelion Foresight mode (multi-agent imagination)
  WS   /ws/law        → AI Law mode (law.go.kr backed citation assistant)
  WS   /ws/chatbot    → Onboarding/guide chatbot (feature recommender)
  POST /api/auth/*    → Login, register, logout, admin
  GET  /api/eng/download/{session_id}   → Download Engineering project zip
  POST /api/eng/upload/{session_id}     → Upload file to Engineering session
  POST /api/datalab/upload              → Upload file to DataLab session
  GET  /api/datalab/download/{sid}/{fn} → Download result from DataLab session
  POST /api/foresight/upload             → Upload file to Foresight session
  GET  /api/foresight/download/{sid}/{fn} → Download result from Foresight session
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.auth.routes import router as auth_router
from src.auth.security import verify_token
from src.persona.routes import router as persona_router
from src.config.settings import get_settings
from src.discussion.session import DiscussionSession
from src.secretary.history_store import HistoryStore
from src.secretary.mode_injector import get_injected_task, get_injector_for_task
from src.secretary.session import SecretarySession
from src.chatbot.session import ChatbotSession
from src.datalab.security import purge_orphan_sessions
from src.ui.sim_runner import SimSession
from src.ui.routes.engineering import router as eng_router
from src.ui.routes.datalab import router as datalab_router
from src.ui.routes.foresight import router as foresight_router
from src.ui.routes.discussion import router as discussion_router
from src.ui.routes.workspace import router as workspace_router
from src.ui.routes.law import router as law_router
from src.ui.routes.dart import router as dart_router

app = FastAPI(title="Enterprise Agent Simulation")

# ── CORS ─────────────────────────────────────────
_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]
if _ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ── Routers ──────────────────────────────────────
app.include_router(auth_router)
app.include_router(persona_router)
app.include_router(eng_router)
app.include_router(datalab_router)
app.include_router(foresight_router)
app.include_router(discussion_router)
app.include_router(workspace_router)
app.include_router(law_router)
app.include_router(dart_router)


# ── Claude Code Usage API ─────────────────────────
import json as _json

_USAGE_FILE = Path("/tmp/claude-usage.json")

# ── 활성 개발의뢰 태스크 레지스트리 ─────────────────────────
# 브라우저 재접속 시 기존 실행 중인 task를 찾아서 stop/manual 같은 시그널을
# 전달하기 위한 용도. state.json이 있는 세션(최초개발)만 등록된다.
import asyncio as _asyncio
_active_dev_tasks: dict[str, _asyncio.Task] = {}


@app.get("/api/usage")
async def get_usage():
    """Claude Code CLI 구독 사용량 반환 (statusline에서 저장한 JSON).

    주의: 이 파일은 statusline hook이 쓰는 사이드 파일이라 외부 CLI가
    돌고 있을 때만 최신. 개발의뢰 모드의 rate limit 자동 재개는 이 파일을
    신뢰하지 않고 자체 추적(state.json)으로 대기 시간을 계산한다.
    """
    if not _USAGE_FILE.exists():
        return {"available": False}
    try:
        data = _json.loads(_USAGE_FILE.read_text())
        data["available"] = True
        return data
    except Exception:
        return {"available": False}


@app.get("/api/dev-sessions/active")
async def get_active_dev_sessions(request: Request):
    """현재 user의 미완료 개발 세션 목록 (최초개발 한정).

    반환 예: [{session_id, state, phase, session_number, next_retry_at,
              backoff_index, guard_remaining, task_preview, updated_at}, ...]
    state='done'|'stopped'|'error' 인 세션은 포함하지 않음.
    """
    import time as _time

    from src.upgrade.dev_state import (
        GUARD_MAX_RETRIES,
        GUARD_WINDOW_SEC,
        scan_all_states,
    )

    user = None
    if _membership_enabled():
        token = request.cookies.get("hq_token", "")
        user = verify_token(token) if token else None
        if not user:
            return JSONResponse({"sessions": []}, status_code=401)
    user_id = user["sub"] if user else ""

    now_ts = _time.time()
    active = []
    for state in scan_all_states():
        if _membership_enabled() and state.user_id and state.user_id != user_id:
            continue
        if state.state in ("done", "stopped", "error"):
            continue
        recent = sum(1 for e in state.rate_limit_history
                     if now_ts - e.at < GUARD_WINDOW_SEC)
        active.append({
            "session_id": state.session_id,
            "state": state.state,
            "phase": state.phase,
            "session_number": state.session_number,
            "next_retry_at": int(state.next_retry_at) if state.next_retry_at else None,
            "backoff_index": state.backoff_index,
            "guard_remaining": max(0, GUARD_MAX_RETRIES - recent),
            "task_preview": state.task[:500],
            "updated_at": state.updated_at,
        })
    active.sort(key=lambda s: s["updated_at"], reverse=True)
    return {"sessions": active}


def _membership_enabled() -> bool:
    """Membership system is active when MEMBERSHIP_ENABLED is True (default)."""
    return get_settings().membership_enabled


def _verify_ws_token(ws: WebSocket) -> dict | None:
    """Extract user from JWT cookie on WebSocket connection."""
    token = ws.cookies.get("hq_token", "")
    if not token:
        return None
    return verify_token(token)


# ── Auth gate (HTTP middleware) ──────────────────
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """JWT-based auth gate — skip if membership not enabled."""
    if not _membership_enabled():
        return await call_next(request)

    path = request.url.path

    # Always allow: health, static assets, auth API
    if path in ("/health", "/favicon.ico") or path.startswith(("/api/auth/", "/reports/")):
        return await call_next(request)

    # Check JWT cookie
    token = request.cookies.get("hq_token", "")
    if token and verify_token(token):
        return await call_next(request)

    # Unauthenticated — serve login page for root, 401 for API
    if path == "/" or path.startswith("/api/"):
        # Root serves index.html which has its own login UI
        if path == "/":
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)


# ── Health check ─────────────────────────────────
@app.get("/health")
async def health():
    result = {"status": "ok", "membership": _membership_enabled()}
    try:
        # Report RSS memory in MB (Linux /proc)
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    result["rss_mb"] = round(rss_kb / 1024, 1)
                    break
    except Exception:
        pass
    return result


_scheduler_service = None

# Active company-builder WebSockets that want cron-triggered run events,
# keyed by user_id. One user may have multiple tabs open, hence a set.
# Populated on connection in company_builder_endpoint; cleaned up on disconnect.
_schedule_event_listeners: "dict[str, set[WebSocket]]" = {}


async def _broadcast_schedule_event(user_id: str, payload: dict) -> None:
    """Fan a schedule_running / schedule_run_complete payload to all
    company-builder sockets currently open for the given user.

    Called from SchedulerService._job_callback when a cron-triggered job
    starts and finishes — without this bridge, the UI's "자동실행 진행 중..."
    overlay only appears for manual "지금 실행" clicks (which emit the same
    payloads directly from the WebSocket handler).
    """
    listeners = _schedule_event_listeners.get(user_id)
    if not listeners:
        return
    dead: list[WebSocket] = []
    for sock in list(listeners):
        try:
            await sock.send_json(payload)
        except Exception:
            dead.append(sock)
    for sock in dead:
        listeners.discard(sock)


@app.on_event("startup")
async def _startup_cleanup():
    """Run retention cleanup on server start."""
    result = HistoryStore.run_retention_cleanup()
    if result["archived"] or result["deleted"]:
        import logging
        logging.getLogger(__name__).info(
            "history_retention: archived=%d, deleted=%d",
            result["archived"], result["deleted"],
        )
    # Cleanup expired discussion reports (24h retention)
    try:
        from src.auth.models import UserDB
        expired = UserDB.get().cleanup_expired_reports()
        if expired:
            import logging
            logging.getLogger(__name__).info(
                "discussion_reports_cleanup: deleted=%d", expired,
            )
    except Exception:
        pass
    # Purge orphan DataLab sessions from previous server runs
    import tempfile
    purged = purge_orphan_sessions(tempfile.gettempdir())
    if purged:
        import logging
        logging.getLogger(__name__).info(
            "datalab_orphan_cleanup: purged=%d", purged,
        )
    # Dandelion expired report cleanup
    from src.ui.routes.foresight import dandelion_cleanup_startup
    await dandelion_cleanup_startup()
    # 개발의뢰 세션 orphan 정리: 이전 서버가 running/waiting 상태로 죽었을 때
    # state.json만 남은 세션을 error로 마크 (asyncio.Task는 서버와 함께 사라짐).
    try:
        from src.upgrade.dev_state import mark_orphans_as_error
        orphans = mark_orphans_as_error()
        if orphans:
            import logging
            logging.getLogger(__name__).info(
                "dev_session_orphan_cleanup: marked=%d", orphans,
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "dev_session_orphan_cleanup_failed: %s", exc,
        )
    # Start scheduler service for cron-based execution
    try:
        from src.scheduler.service import SchedulerService
        global _scheduler_service
        _scheduler_service = SchedulerService()
        _scheduler_service.set_event_callback(_broadcast_schedule_event)
        await _scheduler_service.start()
        import logging
        logging.getLogger(__name__).info("scheduler_service_started")
        # Register UI-saved schedules (JSON-per-user store) with APScheduler.
        # Without this bridge, cron-based auto execution never fires for
        # schedules created via the 스케줄팀 UI.
        try:
            from src.company_builder.scheduler import register_all_company_schedules
            count = register_all_company_schedules(_scheduler_service)
            logging.getLogger(__name__).info(
                "company_schedules_registered: count=%d", count,
            )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "company_schedules_register_failed: %s", e,
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("scheduler_start_failed: %s", e)
        _scheduler_service = None

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    """Serve the simulation UI (no-cache to ensure latest code)."""
    return FileResponse(
        _STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/static/{filename:path}")
async def static_file(filename: str):
    """Serve static assets (images, subdirectories like disc-avatars/)."""
    path = (_STATIC_DIR / filename).resolve()
    if not path.is_relative_to(_STATIC_DIR) or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    cache = "no-cache" if filename.endswith((".js", ".css")) else "public, max-age=86400"
    return FileResponse(path, headers={"Cache-Control": cache})


# ── Discussion report history API ────────────────
def _reconcile_orphan_reports(owner_user_id: str) -> int:
    """Find report files on disk that have no DB row and register them.

    Why: if the WebSocket disconnects between the report node finishing and
    the session emitting disc_report (e.g., browser closed during the
    multi-minute report generation), the file gets written but the DB row
    is never inserted. Without reconciliation, the report becomes invisible
    to the history viewer.

    The report node now writes a sidecar metadata.json (see report.py),
    which gives us topic/participants/style. For pre-existing orphans
    without metadata.json, we fall back to parsing the <h2> from the HTML
    and using sensible defaults.

    Returns the number of orphans backfilled. Best-effort: any single
    failure is logged and skipped.
    """
    import re as _re
    from pathlib import Path as _Path
    from src.auth.models import UserDB

    settings = get_settings()
    base = _Path(settings.report_output_dir)
    if not base.is_dir():
        return 0

    db = UserDB.get()
    backfilled = 0
    with db._conn() as conn:
        for folder in base.glob("disc_*"):
            if not folder.is_dir():
                continue
            report_file = folder / "report.html"
            if not report_file.is_file():
                continue
            session_id = folder.name[len("disc_"):]
            row = conn.execute(
                "SELECT id FROM discussion_reports WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is not None:
                continue  # already registered

            # Try sidecar metadata.json first
            meta_file = folder / "metadata.json"
            topic = ""
            participants: list[str] = []
            style = "free"
            created_at = ""
            if meta_file.is_file():
                try:
                    import json as _json
                    meta = _json.loads(meta_file.read_text(encoding="utf-8"))
                    topic = str(meta.get("topic", "")).strip()
                    participants = list(meta.get("participants", []) or [])
                    style = str(meta.get("style", "free")) or "free"
                    created_at = str(meta.get("created_at", "")) or ""
                except Exception:
                    pass

            # Fallback: parse the <h2> heading from the HTML for the topic
            if not topic:
                try:
                    html_text = report_file.read_text(encoding="utf-8", errors="replace")
                    m = _re.search(r"<h2>(.*?)</h2>", html_text)
                    if m:
                        topic = _re.sub(r"<[^>]+>", "", m.group(1)).strip()
                except Exception:
                    pass
            if not topic:
                topic = "(\uC81C\uBAA9 \uC5C6\uB294 \uD1A0\uB860)"  # "(no title)"

            # Use file mtime if no metadata timestamp available
            if not created_at:
                try:
                    from datetime import datetime as _dt
                    created_at = _dt.fromtimestamp(report_file.stat().st_mtime).isoformat()
                except Exception:
                    from datetime import datetime as _dt
                    created_at = _dt.now().isoformat()

            try:
                from datetime import datetime as _dt, timedelta as _td
                created_dt = _dt.fromisoformat(created_at)
                expires_dt = created_dt + _td(days=db.REPORT_RETENTION_DAYS)
                import json as _json
                conn.execute(
                    "INSERT INTO discussion_reports "
                    "(id, user_id, topic, participants, style, created_at, expires_at, file_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, owner_user_id, topic,
                     _json.dumps(participants, ensure_ascii=False), style,
                     created_dt.isoformat(), expires_dt.isoformat(),
                     f"/reports/{folder.name}/report.html"),
                )
                backfilled += 1
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "report_orphan_backfill_failed: %s (%s)", session_id, e,
                )
    return backfilled


def _resolve_report_user_id(request: Request) -> str | None:
    """Resolve the report owner ID for the current request.

    - Membership enabled: require a valid hq_token cookie.
    - Membership disabled: every caller is treated as the same
      "anonymous" user, matching how reports are saved in
      session.py / models.py:save_discussion_report.

    Returns None if auth is required but missing/invalid.
    """
    if not _membership_enabled():
        return "anonymous"
    token = request.cookies.get("hq_token", "")
    user = verify_token(token) if token else None
    if not user:
        return None
    return user["sub"]


@app.get("/api/reports/discussion")
async def list_discussion_reports(request: Request):
    """List the caller's non-expired discussion reports (7-day window).

    Before listing, scan disk for orphan report files (files that exist
    but have no DB row — typically caused by browser disconnect during
    report generation) and backfill them under the calling user's ID.
    """
    user_id = _resolve_report_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        _reconcile_orphan_reports(user_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("orphan_reconcile_failed: %s", e)
    from src.auth.models import UserDB
    reports = UserDB.get().list_discussion_reports(user_id)
    return JSONResponse({"reports": reports})


@app.delete("/api/reports/discussion/{report_id}")
async def delete_discussion_report(report_id: str, request: Request):
    """Delete a specific discussion report."""
    user_id = _resolve_report_user_id(request)
    if user_id is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from src.auth.models import UserDB
    deleted = UserDB.get().delete_discussion_report(report_id, user_id)
    return JSONResponse({"deleted": deleted})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket endpoint for simulation sessions."""
    await ws.accept()

    # Auth check for WebSocket
    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return

    user_id = user["sub"] if user else ""
    inject_id = ws.query_params.get("inject", "")

    if inject_id:
        injector = get_injector_for_task(inject_id)
        bg = get_injected_task(inject_id)
        if not bg or not injector or bg.mode != "company":
            await ws.send_json({"type": "error", "data": {"message": "Company 세션을 찾을 수 없습니다."}})
            return

        try:
            await injector.subscribe_company(inject_id, ws)
            while True:
                try:
                    msg = await ws.receive_json()
                    if msg.get("type") in ("stop", "disconnect"):
                        break
                except Exception:
                    break
        finally:
            injector.unsubscribe_company(inject_id, ws)
        return

    session = SimSession(ws, user_id=user_id)
    try:
        await session.run()
    except WebSocketDisconnect:
        session.cancel()
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass
        session.cancel()


@app.websocket("/ws/disc")
async def discussion_endpoint(ws: WebSocket):
    """WebSocket endpoint for AI Discussion sessions."""
    await ws.accept()

    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return

    user_id = user["sub"] if user else ""
    inject_id = ws.query_params.get("inject", "")

    if inject_id:
        injector = get_injector_for_task(inject_id)
        bg = get_injected_task(inject_id)
        if not bg or not injector:
            await ws.send_json({"type": "error", "data": {"message": "토론 세션을 찾을 수 없습니다."}})
            return

        try:
            await injector.subscribe_disc(inject_id, ws)
            while True:
                try:
                    msg = await ws.receive_json()
                    if msg.get("type") == "disc_stop":
                        break
                except Exception:
                    break
        finally:
            injector.unsubscribe_disc(inject_id, ws)
        return

    session = DiscussionSession(ws, user_id=user_id)
    try:
        await session.run()
    except WebSocketDisconnect:
        session.cancel()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("disc_endpoint_crash: %s", e, exc_info=True)
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass
        session.cancel()


@app.websocket("/ws/sec")
async def secretary_endpoint(ws: WebSocket):
    """WebSocket endpoint for AI Secretary sessions."""
    await ws.accept()

    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return

    user_id = user["sub"] if user else ""

    session = SecretarySession(ws, user_id=user_id)
    try:
        await session.run()
    except WebSocketDisconnect:
        session.cancel()
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass
        session.cancel()


@app.websocket("/ws/chatbot")
async def chatbot_endpoint(ws: WebSocket):
    """WebSocket endpoint for the onboarding/guide chatbot."""
    await ws.accept()

    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return

    user_id = user["sub"] if user else ""

    session = ChatbotSession(ws, user_id=user_id)
    try:
        await session.run()
    except WebSocketDisconnect:
        session.cancel()
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass
        session.cancel()


@app.websocket("/ws/company-builder")
async def company_builder_endpoint(ws: WebSocket):
    """WebSocket endpoint for Company Builder (team design) sessions."""
    await ws.accept()

    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return

    user_id = user["sub"] if user else ""

    from src.company_builder.builder_agent import BuilderSession, StrategyBuilderSession
    from src.company_builder import storage

    session = BuilderSession(user_id=user_id)
    strategy_session = StrategyBuilderSession(user_id=user_id)

    # Subscribe to scheduler events so the 자동실행 overlay appears for
    # cron-triggered runs as well as manual "지금 실행" clicks.
    _schedule_event_listeners.setdefault(user_id, set()).add(ws)

    try:
        # Send initial lists (companies + strategies)
        companies = storage.list_companies(user_id)
        strategies = storage.list_strategies(user_id)
        await ws.send_json({"type": "builder_companies", "data": {"companies": companies}})
        await ws.send_json({"type": "builder_strategies", "data": {"strategies": strategies}})

        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type", "")

            if msg_type == "builder_message":
                content = msg.get("data", {}).get("content", "")
                workspace_files = msg.get("data", {}).get("workspace_files", [])
                if content:
                    await session.stream_response(content, ws, workspace_files=workspace_files)

            elif msg_type == "save_company":
                company = msg.get("data", {})
                saved = storage.save_company(user_id, company)
                await ws.send_json({"type": "company_saved", "data": saved})
                # Refresh sidebar list
                companies = storage.list_companies(user_id)
                await ws.send_json({"type": "builder_companies", "data": {"companies": companies}})

            elif msg_type == "load_company":
                cid = msg.get("data", {}).get("company_id", "")
                company = storage.load_company(user_id, cid)
                if company:
                    await ws.send_json({"type": "company_loaded", "data": company})
                else:
                    await ws.send_json({"type": "error", "data": {"message": "회사를 찾을 수 없습니다."}})

            elif msg_type == "delete_company":
                cid = msg.get("data", {}).get("company_id", "")
                storage.delete_company(user_id, cid)
                # Cascade: delete related schedules
                try:
                    from src.company_builder import schedule_storage as ss
                    for sched in ss.list_schedules(user_id):
                        if sched.get("company_id") == cid:
                            ss.delete_schedule(user_id, sched["id"])
                except Exception:
                    pass  # non-critical
                await ws.send_json({"type": "company_deleted", "data": {"company_id": cid}})
                # Refresh sidebar list
                companies = storage.list_companies(user_id)
                await ws.send_json({"type": "builder_companies", "data": {"companies": companies}})

            elif msg_type == "validate_task":
                task = msg.get("data", {}).get("task", "")
                team_id = msg.get("data", {}).get("team_id", "")
                company = storage.load_company(user_id, team_id) if team_id else None
                agents = company.get("agents", []) if company else []

                all_companies = storage.list_companies(user_id)
                saved_teams = []
                for co in all_companies:
                    if co.get("id") != team_id:
                        full = storage.load_company(user_id, co["id"])
                        if full:
                            saved_teams.append(full)

                from src.company_builder.team_validator import validate_task_team_fit_async
                result = await validate_task_team_fit_async(task, agents, saved_teams)
                await ws.send_json({"type": "task_validation", "data": result})

            elif msg_type == "list_companies":
                companies = storage.list_companies(user_id)
                await ws.send_json({"type": "builder_companies", "data": {"companies": companies}})

            # ── Schedule operations ──
            elif msg_type == "save_schedule":
                from src.company_builder import schedule_storage as ss
                sched_data = msg.get("data", {})
                saved = ss.save_schedule(user_id, sched_data)
                # Bridge to APScheduler so cron auto-execution actually fires.
                # JSON save already succeeded — we report registration status
                # separately so the user doesn't lose their work on bridge failure.
                registered = False
                register_error = ""
                if _scheduler_service is None:
                    register_error = "scheduler_service_unavailable"
                else:
                    try:
                        from src.company_builder.scheduler import (
                            register_single_schedule,
                            unregister_schedule,
                        )
                        unregister_schedule(_scheduler_service, saved["id"])
                        registered = register_single_schedule(
                            _scheduler_service, user_id, saved["id"],
                        )
                        if not registered and saved.get("enabled", False):
                            register_error = "registration_failed"
                    except Exception as e:
                        register_error = f"{type(e).__name__}: {str(e)[:120]}"
                        import logging
                        logging.getLogger(__name__).warning(
                            "save_schedule bridge failed for %s: %s",
                            saved.get("id"), e,
                        )
                response_data = dict(saved)
                response_data["registered"] = registered
                if register_error:
                    response_data["register_error"] = register_error
                await ws.send_json({"type": "schedule_saved", "data": response_data})

            elif msg_type == "list_schedules":
                from src.company_builder import schedule_storage as ss
                schedules = ss.list_schedules(user_id)
                await ws.send_json({"type": "schedule_list", "data": {"schedules": schedules}})

            elif msg_type == "toggle_schedule":
                from src.company_builder import schedule_storage as ss
                sid = msg.get("data", {}).get("schedule_id", "")
                enabled = msg.get("data", {}).get("enabled", True)
                result = ss.toggle_schedule(user_id, sid, enabled)
                if result:
                    # Bridge: enable → (re)register, disable → unregister
                    if _scheduler_service is not None:
                        try:
                            from src.company_builder.scheduler import (
                                register_single_schedule,
                                unregister_schedule,
                            )
                            unregister_schedule(_scheduler_service, sid)
                            if enabled:
                                register_single_schedule(
                                    _scheduler_service, user_id, sid,
                                )
                        except Exception as e:
                            import logging
                            logging.getLogger(__name__).warning(
                                "toggle_schedule bridge failed for %s: %s", sid, e,
                            )
                    await ws.send_json({"type": "schedule_toggled", "data": result})

            elif msg_type == "delete_schedule":
                from src.company_builder import schedule_storage as ss
                sid = msg.get("data", {}).get("schedule_id", "")
                if _scheduler_service is not None:
                    try:
                        from src.company_builder.scheduler import unregister_schedule
                        unregister_schedule(_scheduler_service, sid)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).warning(
                            "delete_schedule bridge failed for %s: %s", sid, e,
                        )
                ss.delete_schedule(user_id, sid)
                await ws.send_json({"type": "schedule_deleted", "data": {"schedule_id": sid}})

            elif msg_type == "generate_schedule_questions":
                # 고정 질문 대신 상세 설명 입력 폼을 표시하도록 신호 전송
                await ws.send_json({"type": "schedule_detail_prompt", "data": {}})

            elif msg_type == "run_schedule_now":
                sid = msg.get("data", {}).get("schedule_id", "")
                from src.company_builder import schedule_storage as ss
                sched = ss.load_schedule(user_id, sid)
                if sched:
                    await ws.send_json({"type": "schedule_running", "data": {"schedule_id": sid}})
                    try:
                        from src.company_builder.scheduler import _to_scheduled_job
                        from src.scheduler.runner import HeadlessGraphRunner
                        job = _to_scheduled_job(sched, user_id)
                        runner = HeadlessGraphRunner()
                        record = await runner.execute_job(job)

                        status = record.status.value if record.status else "unknown"
                        duration = round(record.duration_seconds or 0, 1)

                        # report_path는 final_state_summary 가 유일한 authoritative source.
                        # (예전에는 thread_id 로 data/reports/{thread_id} 를 추측했지만
                        # 폴더가 이제 "{제목}_{날짜}" 로 네이밍되므로 thread_id 추측 불가)
                        report_path = ""
                        if record.final_state_summary:
                            report_path = record.final_state_summary.get("report_path", "")

                        # 보고서가 있으면 completed로 간주
                        if report_path and status == "running":
                            status = "completed"

                        ss.add_run_record(
                            user_id, sid,
                            run_id=record.execution_id,
                            status=status,
                            report_path=report_path,
                        )
                        await ws.send_json({"type": "schedule_run_complete", "data": {
                            "schedule_id": sid,
                            "status": status,
                            "duration_s": duration,
                        }})
                    except Exception as e:
                        await ws.send_json({"type": "error", "data": {"message": f"실행 실패: {str(e)[:200]}"}})
                    # 목록 갱신
                    schedules = ss.list_schedules(user_id)
                    await ws.send_json({"type": "schedule_list", "data": {"schedules": schedules}})

            # ── Strategy operations (플레이북 설계) ──
            elif msg_type == "strategy_message":
                content = msg.get("data", {}).get("content", "")
                workspace_files = msg.get("data", {}).get("workspace_files", [])
                if content:
                    await strategy_session.stream_response(content, ws, workspace_files=workspace_files)

            elif msg_type == "save_strategy":
                strat = msg.get("data", {})
                saved = storage.save_strategy(user_id, strat)
                await ws.send_json({"type": "strategy_saved", "data": saved})
                strategies = storage.list_strategies(user_id)
                await ws.send_json({"type": "builder_strategies", "data": {"strategies": strategies}})

            elif msg_type == "load_strategy":
                sid = msg.get("data", {}).get("strategy_id", "")
                strat = storage.load_strategy(user_id, sid)
                if strat:
                    await ws.send_json({"type": "strategy_loaded", "data": strat})

            elif msg_type == "delete_strategy":
                sid = msg.get("data", {}).get("strategy_id", "")
                storage.delete_strategy(user_id, sid)
                await ws.send_json({"type": "strategy_deleted", "data": {"strategy_id": sid}})
                strategies = storage.list_strategies(user_id)
                await ws.send_json({"type": "builder_strategies", "data": {"strategies": strategies}})

            elif msg_type == "list_strategies":
                strategies = storage.list_strategies(user_id)
                await ws.send_json({"type": "builder_strategies", "data": {"strategies": strategies}})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("company_builder_crash: %s", e, exc_info=True)
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass
    finally:
        listeners = _schedule_event_listeners.get(user_id)
        if listeners is not None:
            listeners.discard(ws)
            if not listeners:
                _schedule_event_listeners.pop(user_id, None)


# ── 강화소 (Upgrade Station) ─────────────────────────────

@app.websocket("/ws/upgrade")
async def upgrade_endpoint(ws: WebSocket):
    """WebSocket endpoint for 강화소 (기존 앱 업그레이드).

    Message protocol (client → server):
      {type: "start_upgrade_analyze", data: {folder_path, task}}
      {type: "start_upgrade_dev", data: {folder_path, task, answers, backup_path, analysis}}
      {type: "stop_upgrade"}

    Protocol (server → client):
      {type: "upgrade_progress", data: {phase, action, message, ...}}
      {type: "upgrade_activity", data: {tool, label, count}}
      {type: "upgrade_analyze_result", data: {folder_path, backup_path, analysis}}
      {type: "error", data: {message}}
    """
    await ws.accept()

    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return

    user_id = user["sub"] if user else ""

    import asyncio
    import uuid
    from src.modes.common import get_mode_event_queue, run_task_with_stop_listener
    from src.upgrade.runner import prepare_and_analyze, run_upgrade_dev

    _upgrade_task: asyncio.Task | None = None
    _session_id = ""

    async def _drain_events():
        try:
            while True:
                q = get_mode_event_queue(_session_id)
                while not q.empty():
                    ev = q.get_nowait()
                    try:
                        await ws.send_json(ev)
                    except Exception:
                        return
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pass

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type", "")

            if msg_type == "start_upgrade_analyze":
                data = msg.get("data", {})
                folder_path = (data.get("folder_path") or "").strip()
                task = (data.get("task") or "").strip()

                if not folder_path or not task:
                    await ws.send_json({"type": "error", "data": {
                        "message": "폴더 경로와 지시사항이 모두 필요합니다."
                    }})
                    continue

                _session_id = str(uuid.uuid4())[:8]
                drain_task = asyncio.create_task(_drain_events())
                try:
                    result = await prepare_and_analyze(
                        folder_path=folder_path,
                        task=task,
                        session_id=_session_id,
                    )
                    await ws.send_json({
                        "type": "upgrade_analyze_result",
                        "data": {
                            "session_id": _session_id,
                            "folder_path": result["folder_path"],
                            "backup_path": result["backup_path"],
                            "analysis": result["analysis"],
                        },
                    })
                except ValueError as e:
                    await ws.send_json({"type": "error", "data": {"message": str(e)}})
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error("upgrade_analyze_error: %s", e, exc_info=True)
                    await ws.send_json({"type": "error", "data": {
                        "message": f"분석 실패: {str(e)[:300]}"
                    }})
                finally:
                    await asyncio.sleep(0.3)
                    q = get_mode_event_queue(_session_id)
                    while not q.empty():
                        try:
                            await ws.send_json(q.get_nowait())
                        except Exception:
                            break
                    drain_task.cancel()
                    try:
                        await drain_task
                    except asyncio.CancelledError:
                        pass

            elif msg_type == "start_upgrade_dev":
                data = msg.get("data", {})
                folder_path = (data.get("folder_path") or "").strip()
                task = (data.get("task") or "").strip()
                answers = data.get("answers", "") or ""
                backup_path = (data.get("backup_path") or "").strip()
                analysis = data.get("analysis") or {}
                _session_id = data.get("session_id") or str(uuid.uuid4())[:8]

                if not folder_path or not backup_path:
                    await ws.send_json({"type": "error", "data": {
                        "message": "분석 단계의 결과(folder_path, backup_path)가 필요합니다."
                    }})
                    continue

                await ws.send_json({"type": "upgrade_dev_started", "data": {
                    "session_id": _session_id,
                }})

                drain_task = asyncio.create_task(_drain_events())
                _upgrade_task = asyncio.create_task(run_upgrade_dev(
                    folder_path=folder_path,
                    task=task,
                    answers=answers,
                    backup_path=backup_path,
                    analysis=analysis,
                    session_id=_session_id,
                ))
                try:
                    result = await run_task_with_stop_listener(
                        ws, _upgrade_task, {"stop_upgrade"},
                    )
                    if result == "stopped":
                        await ws.send_json({"type": "upgrade_stopped", "data": {}})
                except WebSocketDisconnect:
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error("upgrade_dev_error: %s", e, exc_info=True)
                    try:
                        await ws.send_json({"type": "error", "data": {
                            "message": str(e)[:500]
                        }})
                    except Exception:
                        pass
                finally:
                    await asyncio.sleep(0.5)
                    q = get_mode_event_queue(_session_id)
                    while not q.empty():
                        try:
                            await ws.send_json(q.get_nowait())
                        except Exception:
                            break
                    drain_task.cancel()
                    try:
                        await drain_task
                    except asyncio.CancelledError:
                        pass

            elif msg_type == "stop_upgrade":
                if _upgrade_task and not _upgrade_task.done():
                    _upgrade_task.cancel()
                    await ws.send_json({"type": "upgrade_stopped", "data": {}})

            # ── 최초개발 (0→1) — 개발의뢰 탭의 '최초개발' 서브탭 ──
            elif msg_type == "start_dev_clarify":
                data = msg.get("data", {})
                dev_task = data.get("task", "")
                workspace_files = data.get("workspace_files", [])
                _session_id = str(uuid.uuid4())[:8]

                from src.upgrade.dev_runner import generate_clarify_questions

                drain_task = asyncio.create_task(_drain_events())
                try:
                    questions = await generate_clarify_questions(
                        dev_task, _session_id, workspace_files=workspace_files,
                    )
                    await ws.send_json({
                        "type": "dev_clarify_questions",
                        "data": {"questions": questions, "session_id": _session_id},
                    })
                except Exception as e:
                    await ws.send_json({
                        "type": "error",
                        "data": {"message": f"질문 생성 실패: {e}"},
                    })
                finally:
                    drain_task.cancel()
                    try:
                        await drain_task
                    except asyncio.CancelledError:
                        pass

            elif msg_type == "start_dev":
                data = msg.get("data", {})
                dev_task = data.get("task", "")
                dev_answers = data.get("answers", "")
                dev_session_id = data.get("session_id", str(uuid.uuid4())[:8])
                workspace_files = data.get("workspace_files", [])
                _session_id = dev_session_id

                await ws.send_json({"type": "dev_started", "data": {"session_id": _session_id}})

                from src.upgrade.dev_runner import run_dev_overtime
                from src.upgrade.dev_state import trigger_manual_retry

                drain_task = asyncio.create_task(_drain_events())
                _upgrade_task = asyncio.create_task(
                    run_dev_overtime(
                        task=dev_task,
                        answers=dev_answers,
                        session_id=_session_id,
                        user_id=user_id,
                        overtime_id="",
                        workspace_files=workspace_files,
                    )
                )
                # 재접속 시 찾을 수 있도록 글로벌 레지스트리에 등록
                _active_dev_tasks[_session_id] = _upgrade_task

                def _on_dev_msg(msg):
                    # 실행 중 들어오는 manual_retry 시그널: rate limit 대기 조기 종료
                    if msg.get("type") == "manual_retry":
                        target = (msg.get("data") or {}).get("session_id") or _session_id
                        if target:
                            trigger_manual_retry(target)

                try:
                    result = await run_task_with_stop_listener(
                        ws, _upgrade_task, {"stop_dev"}, on_message=_on_dev_msg,
                    )
                    if result == "stopped":
                        await ws.send_json({"type": "overtime_stopped", "data": {}})
                except WebSocketDisconnect:
                    # 최초개발 세션은 브라우저 닫혀도 서버 태스크를 계속 유지
                    # (state.json + _active_dev_tasks 조합으로 재접속 시 복구 가능).
                    # 강화소는 상위 except에서 취소됨.
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error("dev_initial_error: %s", e, exc_info=True)
                    try:
                        await ws.send_json({"type": "error", "data": {"message": str(e)[:500]}})
                    except Exception:
                        pass
                finally:
                    # 태스크가 종료됐으면 레지스트리에서 제거
                    if _upgrade_task.done():
                        _active_dev_tasks.pop(_session_id, None)
                    await asyncio.sleep(0.5)
                    q = get_mode_event_queue(_session_id)
                    while not q.empty():
                        try:
                            await ws.send_json(q.get_nowait())
                        except Exception:
                            break
                    drain_task.cancel()
                    try:
                        await drain_task
                    except asyncio.CancelledError:
                        pass

            elif msg_type == "observe_dev":
                # 브라우저 재접속 시 기존 실행 중인 개발 세션의 라이브 이벤트를 붙여달라는 요청.
                data = msg.get("data", {})
                target_sid = (data.get("session_id") or "").strip()
                if not target_sid:
                    continue

                from src.upgrade.dev_state import (
                    GUARD_MAX_RETRIES,
                    GUARD_WINDOW_SEC,
                    DevState as _DS,
                    trigger_manual_retry as _trigger_mr,
                )
                import time as _time

                state_path = _DS.path_for(target_sid)
                if not state_path.exists():
                    await ws.send_json({"type": "error", "data": {
                        "message": "세션을 찾을 수 없어요."
                    }})
                    continue

                try:
                    loaded_state = _DS.load(state_path)
                except Exception:
                    await ws.send_json({"type": "error", "data": {
                        "message": "세션 상태 파일이 손상됐어요."
                    }})
                    continue

                if _membership_enabled() and loaded_state.user_id and loaded_state.user_id != user_id:
                    await ws.send_json({"type": "error", "data": {
                        "message": "이 세션에 접근할 권한이 없어요."
                    }})
                    continue

                _session_id = target_sid
                now_ts = _time.time()
                recent = sum(1 for e in loaded_state.rate_limit_history
                             if now_ts - e.at < GUARD_WINDOW_SEC)

                # 현재 state snapshot을 UI에 주입 — 카운트다운/Phase bar 복원용
                await ws.send_json({"type": "dev_session_restore", "data": {
                    "session_id": target_sid,
                    "state": loaded_state.state,
                    "phase": loaded_state.phase,
                    "session_number": loaded_state.session_number,
                    "next_retry_at": int(loaded_state.next_retry_at) if loaded_state.next_retry_at else None,
                    "backoff_index": loaded_state.backoff_index,
                    "guard_remaining": max(0, GUARD_MAX_RETRIES - recent),
                    "task_preview": loaded_state.task[:500],
                    "dev_complete": loaded_state.dev_complete,
                }})

                # 서버 태스크가 아직 돌고 있으면 그것을 await, 아니면 drain만.
                existing_task = _active_dev_tasks.get(target_sid)
                drain_task = asyncio.create_task(_drain_events())

                def _on_obs_msg(msg):
                    if msg.get("type") == "manual_retry":
                        _trigger_mr(target_sid)

                try:
                    if existing_task is not None and not existing_task.done():
                        _upgrade_task = existing_task
                        result = await run_task_with_stop_listener(
                            ws, existing_task, {"stop_dev"}, on_message=_on_obs_msg,
                        )
                        if result == "stopped":
                            await ws.send_json({"type": "overtime_stopped", "data": {}})
                    else:
                        # 태스크 없음 (서버 재시작 등). state.json만 존재.
                        # stop_dev 받으면 state='stopped'로만 마크, 그 외 메시지는 on_msg로 처리.
                        async def _listen_stop_only():
                            while True:
                                m = await ws.receive_json()
                                mtype = m.get("type")
                                if mtype == "stop_dev":
                                    loaded_state.state = "stopped"
                                    loaded_state.error_reason = "user_stopped_no_task"
                                    loaded_state.save(state_path)
                                    await ws.send_json({"type": "overtime_stopped", "data": {}})
                                    return
                                if mtype == "manual_retry":
                                    # 태스크가 없으니 트리거해도 아무도 안 깨어남.
                                    # 알려주기만 하고 스킵.
                                    await ws.send_json({"type": "error", "data": {
                                        "message": "진행 중인 태스크가 없어서 재시도할 수 없어요 (서버 재시작됐을 수 있어요)."
                                    }})
                        await _listen_stop_only()
                except WebSocketDisconnect:
                    # 재접속도 그냥 끊기면 그만 — 상위에서 task cancel은 안 함.
                    raise
                finally:
                    if existing_task is not None and existing_task.done():
                        _active_dev_tasks.pop(target_sid, None)
                    await asyncio.sleep(0.5)
                    q = get_mode_event_queue(target_sid)
                    while not q.empty():
                        try:
                            await ws.send_json(q.get_nowait())
                        except Exception:
                            break
                    drain_task.cancel()
                    try:
                        await drain_task
                    except asyncio.CancelledError:
                        pass

            elif msg_type == "stop_dev":
                # 로컬 _upgrade_task가 있으면 직접, 없으면 글로벌 레지스트리에서 찾음.
                target_task = _upgrade_task
                if target_task is None or target_task.done():
                    target_task = _active_dev_tasks.get(_session_id) if _session_id else None
                if target_task and not target_task.done():
                    target_task.cancel()
                    await ws.send_json({"type": "overtime_stopped", "data": {}})

            elif msg_type == "manual_retry":
                # start_dev 흐름 바깥에서 직접 들어온 경우 (드문 케이스지만 안전망).
                from src.upgrade.dev_state import trigger_manual_retry as _tmr
                target_sid = (msg.get("data") or {}).get("session_id") or _session_id
                if target_sid:
                    _tmr(target_sid)

    except WebSocketDisconnect:
        # 최초개발(state.json이 있는 세션)은 브라우저 닫혀도 서버 태스크를 유지한다.
        # 재접속 시 observe_dev로 다시 붙고, 그 사이엔 asyncio.Task가 계속 동작.
        # 강화소/기타 legacy flow는 기존대로 취소 (state 저장 없음 → 재개 불가).
        if _upgrade_task and not _upgrade_task.done():
            from src.upgrade.dev_state import DevState as _DS
            is_state_aware = bool(_session_id) and _DS.path_for(_session_id).exists()
            if not is_state_aware:
                _upgrade_task.cancel()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("upgrade_crash: %s", e, exc_info=True)


@app.get("/api/skill-builder/list")
async def skill_builder_list():
    """스킬 목록 패널이 호출. data/skills/registry.json 내용을 반환."""
    from dataclasses import asdict
    from pathlib import Path as _Path

    from src.skill_builder.registry import SkillRegistry

    reg = SkillRegistry(path=_Path("data/skills/registry.json"))
    return {"skills": [asdict(r) for r in reg.list_all()]}


@app.websocket("/ws/skill-builder")
async def skill_builder_endpoint(ws: WebSocket):
    """스킬 만들기 패널의 세션 resume 루프 플로우.

    Message protocol (client → server):
      {type: "start", data: {description: str}}
      {type: "choice", data: {choice: "new" | "import:<slug>"}}
      {type: "user_message", data: {text: str}}  # skill-creator 인터뷰 답변
      {type: "cancel"}

    Message protocol (server → client):
      {type: "greeting", data: {text: str}}
      {type: "search_results", data: {candidates: [...]}}
      {type: "assistant_message", data: {text: str}}
      {type: "created", data: {slug: str, skill_path: str}}
      {type: "error", data: {message: str}}
    """
    await ws.accept()
    from src.skill_builder.runner import run_skill_builder_session

    try:
        await run_skill_builder_session(ws)
    except WebSocketDisconnect:
        return
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(
            "skill_builder_crash: %s", e, exc_info=True
        )
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass


@app.get("/api/skill-builder/runs/{slug}")
async def skill_builder_runs(slug: str):
    """특정 스킬의 실행 이력을 newest first로 반환."""
    from dataclasses import asdict

    from fastapi.responses import JSONResponse

    from src.skill_builder.run_history import list_runs

    try:
        records = list_runs(slug)
    except ValueError:
        return JSONResponse({"error": "invalid slug"}, status_code=400)
    return {"runs": [asdict(r) for r in records]}


@app.delete("/api/skill-builder/skills/{slug}")
async def skill_builder_delete(slug: str):
    """스킬 삭제: registry에서 제거 + 디스크 파일 삭제."""
    import shutil
    from pathlib import Path as _Path

    from fastapi.responses import JSONResponse

    from src.skill_builder.registry import SkillRegistry

    reg = SkillRegistry(path=_Path("data/skills/registry.json"))
    removed = reg.remove(slug)
    if removed is None:
        return JSONResponse({"error": "스킬을 찾을 수 없습니다"}, status_code=404)

    skill_dir = _Path(removed.skill_path)
    if skill_dir.exists() and skill_dir.is_dir():
        shutil.rmtree(skill_dir, ignore_errors=True)

    return {"ok": True, "slug": slug}


@app.get("/api/skill-builder/skills/{slug}/body")
async def skill_builder_get_body(slug: str):
    """스킬 SKILL.md 본문 조회 (편집용)."""
    from pathlib import Path as _Path

    from fastapi.responses import JSONResponse

    from src.skill_builder.registry import SkillRegistry

    reg = SkillRegistry(path=_Path("data/skills/registry.json"))
    matching = [r for r in reg.list_all() if r.slug == slug]
    if not matching:
        return JSONResponse({"error": "스킬을 찾을 수 없습니다"}, status_code=404)

    skill_md = _Path(matching[0].skill_path) / "SKILL.md"
    if not skill_md.exists():
        return JSONResponse({"error": "SKILL.md 파일 없음"}, status_code=404)

    return {"slug": slug, "body": skill_md.read_text(encoding="utf-8")}


@app.put("/api/skill-builder/skills/{slug}/body")
async def skill_builder_update_body(slug: str, request: Request):
    """스킬 SKILL.md 본문 저장 (편집용)."""
    from pathlib import Path as _Path

    from fastapi.responses import JSONResponse

    from src.skill_builder.registry import SkillRegistry

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    body = data.get("body", "")
    if not body.strip():
        return JSONResponse({"error": "본문이 비어있습니다"}, status_code=400)

    reg = SkillRegistry(path=_Path("data/skills/registry.json"))
    matching = [r for r in reg.list_all() if r.slug == slug]
    if not matching:
        return JSONResponse({"error": "스킬을 찾을 수 없습니다"}, status_code=404)

    skill_md = _Path(matching[0].skill_path) / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(body, encoding="utf-8")

    # frontmatter에서 name 추출하여 registry 갱신
    name = matching[0].name
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if line.strip().startswith("description:"):
                    name = line.split(":", 1)[1].strip()[:50]
                    break
    reg.update(slug, name=name)

    return {"ok": True, "slug": slug}


@app.websocket("/ws/skill-execute")
async def skill_execute_endpoint(ws: WebSocket):
    """스킬 카드 실행 — single shot 실행 후 종료.

    Protocol (client → server):
      {type: "execute", data: {slug: str, user_input: str}}
      {type: "cancel"}

    Protocol (server → client):
      {type: "started"}
      {type: "tool_use", data: {tool, tool_count, elapsed}}
      {type: "text", data: {chunk, elapsed}}
      {type: "timeout", data: {elapsed}}
      {type: "completed", data: {run_id, result_text, ...}}
      {type: "error", data: {message}}
    """
    import asyncio

    from src.skill_builder.execution_runner import run_skill

    await ws.accept()
    task = None  # Track runner so we can cancel on disconnect
    try:
        msg = await ws.receive_json()
        if msg.get("type") != "execute":
            await ws.send_json({
                "type": "error",
                "data": {"message": "첫 메시지는 execute 타입이어야 합니다"},
            })
            return

        data = msg.get("data") or {}
        slug = (data.get("slug") or "").strip()
        user_input = data.get("user_input") or ""
        workspace_files = data.get("workspace_files", [])

        from src.utils.workspace import read_files_as_context
        file_ctx = read_files_as_context("skill", workspace_files) if workspace_files else ""
        effective_input = user_input
        if file_ctx:
            effective_input = user_input + "\n\n" + file_ctx

        if not slug:
            await ws.send_json({
                "type": "error",
                "data": {"message": "slug는 필수입니다"},
            })
            return

        pending: list[dict] = []

        def on_event(ev: dict) -> None:
            # streamer는 동기 콜백을 호출 — 큐에 쌓아두고 메인 task가 flush
            pending.append(ev)

        async def flush_pending() -> None:
            while pending:
                ev = pending.pop(0)
                action = ev.get("action")
                if action == "tool_use":
                    await ws.send_json({"type": "tool_use", "data": ev})
                elif action == "text":
                    await ws.send_json({"type": "text", "data": ev})
                elif action == "started":
                    await ws.send_json({"type": "started"})
                elif action == "timeout":
                    await ws.send_json({"type": "timeout", "data": ev})
                elif action == "error":
                    await ws.send_json({"type": "error", "data": ev})
                # 'completed'는 run_skill 반환 후 record와 함께 보냄

        async def runner_task():
            return await run_skill(
                slug=slug,
                user_input=effective_input,
                on_event=on_event,
            )

        task = asyncio.create_task(runner_task())
        # 50ms마다 flush — 실시간성 vs 오버헤드 균형
        while not task.done():
            await flush_pending()
            await asyncio.sleep(0.05)
        await flush_pending()
        record = await task

        await ws.send_json({
            "type": "completed",
            "data": {
                "run_id": record.run_id,
                "result_text": record.result_text,
                "tool_count": record.tool_count,
                "duration_seconds": record.duration_seconds,
                "status": record.status,
                "error_message": record.error_message,
            },
        })
    except WebSocketDisconnect:
        # Cancel orphaned runner task so subprocess is torn down
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return
    except Exception as e:
        if task and not task.done():
            task.cancel()
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass


@app.websocket("/ws/agent")
async def agent_mode_endpoint(ws: WebSocket):
    """WebSocket endpoint for AI Agent mode sessions."""
    await ws.accept()
    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return
    user_id = user["sub"] if user else ""

    from src.agent_mode.session import AgentSession
    session = AgentSession(ws, user_id=user_id)
    try:
        await session.run()
    except WebSocketDisconnect:
        session.cancel()
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass
        session.cancel()


# ── Persona Workshop interview ─────────────────

@app.websocket("/ws/persona")
async def persona_interview_endpoint(ws: WebSocket):
    """WebSocket endpoint for persona interview sessions."""
    await ws.accept()
    user = _verify_ws_token(ws) if _membership_enabled() else None
    if _membership_enabled() and not user:
        await ws.send_json({"type": "error", "data": {"message": "인증이 필요합니다."}})
        await ws.close(code=4001)
        return
    user_id = user["sub"] if user else ""

    from src.persona.interview import InterviewSession
    session = InterviewSession(ws, user_id=user_id)
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass


# ── Engineering/DataLab/Foresight/Dandelion routes → src/ui/routes/ ──
# (추출 완료 — eng_router, datalab_router, foresight_router로 include됨)



_REPORTS_DIR = Path(__file__).parents[2] / "data" / "reports"
_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
_DATA_DIR = Path(__file__).parents[2] / "data"

# ── Workspace startup init ────────────────────────
from src.utils.workspace import ensure_workspace, VALID_MODES  # noqa: E402
for _m in VALID_MODES:
    ensure_workspace(_m)


@app.get("/preview-file")
async def preview_file(path: str):
    """Serve a file for inline preview (restricted to data/ directory)."""
    resolved = Path(path).resolve()
    if not str(resolved).startswith(str(_DATA_DIR.resolve())):
        return HTMLResponse("<p>Access denied</p>", status_code=403)
    if not resolved.exists() or not resolved.is_file():
        return HTMLResponse("<p>File not found</p>", status_code=404)
    suffix = resolved.suffix.lower()
    if suffix in (".html", ".htm"):
        return FileResponse(resolved, media_type="text/html")
    if suffix == ".md":
        return FileResponse(resolved, media_type="text/plain")
    return FileResponse(resolved)


@app.post("/api/pick-folder")
async def pick_folder():
    """Open a native folder picker dialog and return the absolute path.

    Uses macOS osascript / Linux zenity. Browsers don't expose absolute paths
    for dragged folders, so this is the reliable way to capture them.
    """
    import subprocess as _sp
    import platform
    try:
        if platform.system() == "Darwin":
            script = 'POSIX path of (choose folder with prompt "업그레이드 대상 폴더를 선택하세요")'
            proc = _sp.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                # User cancelled the dialog → returncode 1, stderr "User canceled."
                return {"ok": False, "cancelled": True}
            path = proc.stdout.strip().rstrip("/")
            return {"ok": True, "path": path} if path else {"ok": False, "cancelled": True}
        elif platform.system() == "Linux":
            proc = _sp.run(
                ["zenity", "--file-selection", "--directory"],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                return {"ok": False, "cancelled": True}
            return {"ok": True, "path": proc.stdout.strip()}
        else:
            return {"ok": False, "error": "unsupported platform"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"picker tool missing: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/open-folder")
async def open_folder(request: Request):
    """Open a local folder in Finder/Explorer."""
    data = await request.json()
    folder_path = data.get("path", "")
    if not folder_path:
        return {"ok": False, "error": "path required"}
    from pathlib import Path as _P
    resolved = _P(folder_path).resolve()
    if not resolved.exists():
        return {"ok": False, "error": "path not found"}
    import subprocess as _sp
    import platform
    try:
        if platform.system() == "Darwin":
            _sp.Popen(["open", str(resolved)])
        elif platform.system() == "Windows":
            _sp.Popen(["explorer", str(resolved)])
        else:
            _sp.Popen(["xdg-open", str(resolved)])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


@app.get("/reports/{session_id}")
async def report_main(session_id: str):
    """Serve the main report HTML or a folder listing from a session directory.

    Tries filenames in priority order, then dated files, then folder listing.
    """
    import glob as glob_mod
    session_dir = (_REPORTS_DIR / session_id).resolve()
    if not str(session_dir).startswith(str(_REPORTS_DIR.resolve())):
        return HTMLResponse("<p>Access denied</p>", status_code=403)
    if not session_dir.exists():
        return HTMLResponse("<p>Report not found</p>", status_code=404)

    # 1) 고정 파일명 우선 탐색
    for name in ("results.html", "result.html", "result_whole.html", "report.html"):
        candidate = session_dir / name
        if candidate.exists():
            return FileResponse(candidate, media_type="text/html")

    # 2) 날짜 파일명 (최신순)
    dated = sorted(glob_mod.glob(str(session_dir / "results_*.html")), reverse=True)
    if dated:
        return FileResponse(dated[0], media_type="text/html")

    # 3) 폴더 리스팅 (파일 목록 페이지)
    files = sorted(session_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
    if files:
        import html as html_mod
        rows = []
        for f in files:
            if not f.is_file():
                continue
            name = html_mod.escape(f.name)
            size = f.stat().st_size
            size_str = f"{size / 1024:.1f} KB" if size > 1024 else f"{size} B"
            url = f"/reports/{session_id}/{f.name}"
            rows.append(f'<tr><td><a href="{url}">{name}</a></td><td>{size_str}</td></tr>')
        title = html_mod.escape(session_id)
        return HTMLResponse(
            f'<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">'
            f'<title>{title}</title>'
            f'<style>body{{font-family:sans-serif;max-width:700px;margin:40px auto;padding:0 20px}}'
            f'table{{width:100%;border-collapse:collapse}}td,th{{padding:8px 12px;text-align:left;border-bottom:1px solid #eee}}'
            f'a{{color:#0066cc;text-decoration:none}}a:hover{{text-decoration:underline}}</style></head>'
            f'<body><h1>{title}</h1><table><tr><th>파일</th><th>크기</th></tr>{"".join(rows)}</table></body></html>'
        )

    return HTMLResponse("<p>Report not found</p>", status_code=404)


app.mount("/reports", StaticFiles(directory=str(_REPORTS_DIR), html=False), name="reports")


@app.get("/apps/{session_id}/guide")
async def dev_app_guide(session_id: str):
    """Serve the generated app's guide.html (located inside the app folder)."""
    base = (_DATA_DIR / "workspace" / "overtime" / "output").resolve()
    app_dir = (base / session_id / "app").resolve()
    if not str(app_dir).startswith(str(base)):
        return HTMLResponse("<p>Access denied</p>", status_code=403)
    guide = app_dir / "guide.html"
    if not guide.exists():
        return HTMLResponse("<p>Guide not found</p>", status_code=404)
    return FileResponse(guide, media_type="text/html")


# ── Advisory endpoint (separate from main pipeline) ──

from pydantic import BaseModel as _PydanticBase


class _AdvisoryRequest(_PydanticBase):
    report_path: str
    session_id: str
    persona_ids: list[str]
    user_id: str = ""


@app.post("/api/advisory")
async def advisory_endpoint(req: _AdvisoryRequest):
    """Generate advisory comments from personas on a completed report."""
    from src.persona.advisory import generate_advisory_comments

    # Read report HTML from report directory
    report_text = ""
    rp = Path(req.report_path)
    if rp.is_dir():
        for name in ("results.html", "result.html", "result_whole.html"):
            candidate = rp / name
            if candidate.exists():
                try:
                    report_text = candidate.read_text(encoding="utf-8")
                except Exception:
                    pass
                break

    if not report_text:
        return {"comments": [], "error": "Report not found"}

    # Use actual user_id for persona access control (fallback to session_id)
    uid = req.user_id or req.session_id

    try:
        comments = await generate_advisory_comments(
            report_text=report_text,
            persona_ids=req.persona_ids,
            user_id=uid,
        )
        return {"comments": comments}
    except Exception as e:
        return {"comments": [], "error": str(e)}



def start_sim_server(host: str = "0.0.0.0", port: int = 8420):
    """Start the simulation UI server and auto-open the browser."""
    import threading
    import webbrowser

    import uvicorn
    from rich.console import Console

    console = Console()
    url = f"http://{host}:{port}"

    console.print()
    console.print(f"  [bold magenta]Enterprise HQ[/bold magenta] — Corporate Simulation UI")
    console.print(f"  [dim]Server:[/dim] {url}")
    if _membership_enabled():
        console.print(f"  [dim]Membership:[/dim] enabled")
    console.print(f"  [dim]Press Ctrl+C to stop[/dim]")
    console.print()

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")
