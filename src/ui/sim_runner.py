"""Session runner — bridges LangGraph execution with WebSocket clients.

Each WebSocket connection gets its own SimSession, which:
1. Receives a task from the browser
2. Runs the LangGraph graph
3. Translates graph events to SimEvents via EventBridge
4. Sends SimEvents to the browser
5. Handles interrupts (questions, reviews) via WebSocket roundtrip
"""

from __future__ import annotations

import asyncio
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

from pathlib import Path

from src.engine import SqliteCheckpointer, ResumeCommand

from src.config.settings import get_settings
from src.graphs.main_graph import build_pipeline
from src.models.state import create_initial_state
from src.ui.event_bridge import EventBridge
from src.modes.common import get_mode_event_queue, cleanup_mode_event_queue
# Subprocess management — lazy import to avoid circular deps
# In API mode these are no-ops; in CLI mode they manage subprocess PIDs.
def set_session_tag(tag: str) -> None:
    if not get_settings().use_api_direct:
        from src.utils.claude_code import set_session_tag as _fn
        _fn(tag)

def get_pids_by_session(sid: str) -> set:
    if not get_settings().use_api_direct:
        from src.utils.claude_code import get_pids_by_session as _fn
        return _fn(sid)
    return set()

def cleanup_specific_pids(pids: set) -> None:
    if not get_settings().use_api_direct:
        from src.utils.claude_code import cleanup_specific_pids as _fn
        _fn(pids)
from src.utils.execution_tracker import reset_exec_tracker
from src.utils.progress import get_tracker, get_step_tracker, compute_progress, NODE_LABELS, _NODE_TAU, _MAX_SIMULATED


class SimSession:
    """One WebSocket connection = one graph session."""

    def __init__(self, ws, user_id: str = ""):
        self.ws = ws
        self.bridge = EventBridge()
        self._graph_task: asyncio.Task | None = None
        self._progress_task: asyncio.Task | None = None
        self._step_progress_task: asyncio.Task | None = None
        self._mode_drain_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._interrupt_future: asyncio.Future | None = None
        self._cancelled = False
        self._session_id: str = ""
        self._user_id: str = user_id
        self._team_id: str = ""
        self._strategy: dict | None = None
        self._output_format: str = "html"
        self._workspace_files: list[str] = []
        self._workspace_mode: str = "instant"

    async def run(self):
        """Main loop: send init, idle until start, run graph, handle interrupts."""
        # 1. Send initial layout (browser can render idle office immediately)
        layout = self.bridge.floor.get_layout()
        characters = self.bridge.characters.get_all_active()
        await self._send({
            "type": "init",
            "data": {
                "layout": layout,
                "characters": characters,
            },
        })

        # 2. Start heartbeat to keep WS alive during idle browsing
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # 3. Wait for 'start' message (non-blocking idle — browser shows office)
        user_task = None
        while not self._cancelled:
            try:
                msg = await self.ws.receive_json()
            except Exception:
                break
            if msg.get("type") == "start" and msg.get("task"):
                user_task = msg["task"]
                self._team_id = msg.get("team_id", "")
                self._strategy = msg.get("strategy")  # 전략 프리셋
                self._output_format = msg.get("output_format", "html")
                self._workspace_files = msg.get("workspace_files", [])
                self._workspace_mode = msg.get("workspace_mode", "instant")
                print(f"[SIM-START] task='{user_task[:50]}', team_id='{self._team_id}', strategy={'yes' if self._strategy else 'no'}, format={self._output_format}, user_id='{self._user_id}'")
                break

        if not user_task:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            return

        # 4. Run graph, listen for input concurrently
        self._graph_task = asyncio.create_task(self._run_graph(user_task))
        listen_task = asyncio.create_task(self._listen_for_input())

        try:
            await self._graph_task
        except Exception as e:
            import traceback
            print(f"[GRAPH-TASK-ERROR] {type(e).__name__}: {e}")
            traceback.print_exc()
            await self._send({"type": "error", "data": {"message": f"{type(e).__name__}: {str(e)[:500]}"}})
        finally:
            listen_task.cancel()
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            try:
                await listen_task
            except asyncio.CancelledError:
                pass
            if self._heartbeat_task:
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
            # Stop mode event drain task
            self._stop_mode_event_drain()
            # Always cleanup mode event queue on all exit paths (normal, error, cancel)
            if self._session_id:
                cleanup_mode_event_queue(self._session_id)

    async def _run_graph(self, user_task: str):
        """Execute the graph and stream events to the browser."""
        settings = get_settings()
        db_path = settings.checkpoint_db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        thread_id = str(uuid.uuid4())[:8]
        self._session_id = thread_id
        config = {"configurable": {"thread_id": thread_id}}

        set_session_tag(f"company_{thread_id}")
        get_step_tracker(thread_id).start_pipeline()
        reset_exec_tracker()

        async with SqliteCheckpointer(db_path) as checkpointer:
            app = build_pipeline(checkpointer=checkpointer)

            # Build pre_context from saved team or strategy
            pre_context = {}
            execution_mode = "interactive"
            if self._strategy:
                pre_context = {"strategy": self._strategy, "output_format": self._output_format}
                execution_mode = "interactive"
                print(f"[STRATEGY-MODE] Using strategy '{self._strategy.get('name', '?')}'")
            elif self._team_id:
                pre_context = self._build_team_pre_context(self._team_id, self._user_id)
                if pre_context.get("team_agents"):
                    execution_mode = "team"
                    print(f"[TEAM-MODE] Loaded team '{pre_context.get('team_name')}' with {len(pre_context['team_agents'])} agents")

            # output_format을 pre_context에 항상 포함
            if self._output_format != "html":
                pre_context["output_format"] = self._output_format

            from src.utils.workspace import read_files_as_context

            effective_task = user_task
            if self._workspace_files:
                file_ctx = read_files_as_context(
                    self._workspace_mode, self._workspace_files
                )
                if file_ctx:
                    effective_task = user_task + "\n\n" + file_ctx

            initial = create_initial_state(
                effective_task, session_id=thread_id,
                execution_mode=execution_mode,
                pre_context=pre_context,
            )

            # Stream graph events
            await self._stream_graph(app, initial, config)

            # Handle subsequent interrupts
            while not self._cancelled:
                snapshot = await app.aget_state(config)
                if not snapshot.next:
                    break

                interrupt_data = self._extract_interrupt(snapshot)
                if not interrupt_data:
                    break

                # FIX: Future를 먼저 생성 후 전송 (브라우저 즉시 응답 시 유실 방지)
                self._interrupt_future = asyncio.get_running_loop().create_future()
                await self._send({"type": "interrupt", "data": interrupt_data})

                try:
                    user_response = await asyncio.wait_for(
                        self._interrupt_future, timeout=600  # 10min max wait
                    )
                except asyncio.TimeoutError:
                    await self._send({
                        "type": "error",
                        "data": {"message": "인터럽트 응답 타임아웃 (10분)"},
                    })
                    break

                # Resume graph with user response
                print(f"[RESUME] Resuming graph after interrupt, thread={thread_id}, response_type={type(user_response).__name__}")
                # 싱글 세션 모드: resume 시 mode event drain 시작 (활동 스트리밍)
                self._start_mode_event_drain()
                try:
                    await self._stream_graph(app, ResumeCommand(value=user_response), config)
                except Exception as resume_err:
                    import traceback
                    print(f"[RESUME-ERROR] Graph resume failed: {type(resume_err).__name__}: {resume_err}")
                    traceback.print_exc()
                    await self._send({
                        "type": "error",
                        "data": {"message": f"그래프 재개 실패: {type(resume_err).__name__}: {str(resume_err)[:300]}"},
                    })
                    break

            # Grab report path + report text while checkpointer is still open
            report_path = ""
            try:
                snapshot = await app.aget_state(config)
                if snapshot and snapshot.values:
                    report_path = snapshot.values.get("report_file_path", "")
            except Exception:
                pass

        # If no report was generated, try to create a minimal one
        if not report_path:
            try:
                from pathlib import Path as _Path
                fallback_dir = _Path("data/reports") / thread_id
                fallback_dir.mkdir(parents=True, exist_ok=True)
                fallback_file = fallback_dir / "results.html"
                if not fallback_file.exists():
                    fallback_file.write_text(
                        "<html><body><h1>Report</h1>"
                        "<p>작업이 완료되었으나 보고서 생성에 실패했습니다.</p>"
                        "</body></html>",
                        encoding="utf-8",
                    )
                report_path = str(fallback_dir)
                print(f"[COMPLETE] Created fallback report at {report_path}")
            except Exception:
                pass

        # Convert filesystem path to URL path for browser
        # data/reports/{session_id} → /reports/{session_id}
        url_path = ""
        if report_path:
            from pathlib import PurePosixPath
            parts = PurePosixPath(report_path).parts
            try:
                idx = parts.index("reports")
                url_path = "/reports/" + "/".join(parts[idx + 1:])
            except ValueError:
                url_path = report_path  # fallback: 원본 경로

        # Resolve absolute local path for "open folder" feature
        local_abs_path = ""
        if report_path:
            from pathlib import Path as _P
            resolved = _P(report_path).resolve()
            if resolved.exists():
                local_abs_path = str(resolved)

        # Send completion with report path
        await self._send({
            "type": "complete",
            "data": {
                "session_id": thread_id,
                "report_path": url_path,
                "local_path": local_abs_path,
            },
        })
        print(f"[COMPLETE] Sent complete event, report_path={url_path}")

        # Cleanup mode event queue
        cleanup_mode_event_queue(self._session_id)

    async def _stream_graph(self, app, input_data, config):
        """Stream graph events, translate via EventBridge, send to browser."""
        tracker = get_tracker(self._session_id)

        async for event in app.astream(input_data, config=config):
            if self._cancelled:
                break
            for node_name, update in event.items():
                if not isinstance(update, dict):
                    continue

                # Pre-translate: progress tracking (non-critical)
                phase = update.get("phase", "")
                try:
                    # Stop step progress polling when non-worker node completes
                    _STOP_POLLING_NODES = (
                        "ceo_final_report", "worker_result_revision",
                        "deep_research", "breadth_research",
                    )
                    if node_name in _STOP_POLLING_NODES:
                        self._stop_step_progress_polling()

                    print(f"[DEBUG-PROGRESS] node={node_name} phase={phase}")
                    # Start progress polling when task decomposition emits workers
                    if node_name == "ceo_task_decomposition" and "workers" in update:
                        self._start_progress_polling()
                except Exception as ev_err:
                    print(f"[STREAM-EVENT-ERROR] node={node_name} error={type(ev_err).__name__}: {ev_err}")

                # Translate and send (processes hierarchy event)
                try:
                    sim_events = self.bridge.translate(node_name, update)
                    for se in sim_events:
                        await self._send(se.to_dict())
                except Exception as translate_err:
                    import traceback
                    print(f"[TRANSLATE-ERROR] node={node_name} error={type(translate_err).__name__}: {translate_err}")
                    traceback.print_exc()

                # Note: mode event queue is drained by _poll_mode_events background task
                # (started on mode_dispatch, stopped on mode execution completion)

                # Post-translate: announce next step (non-critical)
                try:
                    if node_name == "worker_execution":
                        print(f"[DEBUG-PROGRESS] {node_name} node completed, stopping tracker")
                        tracker.stop()
                        self._announce_next_step("ceo_final_report")
                except Exception as post_err:
                    print(f"[POST-TRANSLATE-ERROR] node={node_name} error={type(post_err).__name__}: {post_err}")

        # Stop progress polling
        self._stop_progress_polling()
        self._stop_step_progress_polling()

    def _start_progress_polling(self):
        if self._progress_task is None or self._progress_task.done():
            print("[DEBUG-PROGRESS] Starting progress polling task")
            self._progress_task = asyncio.create_task(self._poll_worker_progress())
        else:
            print("[DEBUG-PROGRESS] Poll task already running, skipping")

    def _stop_progress_polling(self):
        if self._progress_task and not self._progress_task.done():
            self._progress_task.cancel()

    def _announce_next_step(self, node_name: str):
        """Pre-emptively send scene_change + start step progress polling.

        Called when we know the next node is about to start (e.g., after
        worker_execution completes, ceo_final_report is next). This fills
        the gap where the node is running but hasn't emitted any events yet.
        """
        import math
        self.bridge._step += 1
        self.bridge._current_node = node_name
        label = NODE_LABELS.get(node_name, node_name)
        tau = _NODE_TAU.get(node_name, 30)

        # Send scene_change immediately
        asyncio.ensure_future(self._send({
            "type": "scene_change",
            "ts": time.time(),
            "data": {"node": node_name, "label": label, "step": self.bridge._step},
        }))

        # Start step-level progress polling
        if tau > 0:
            self._stop_step_progress_polling()
            self._step_progress_task = asyncio.create_task(
                self._poll_step_progress(node_name, label, tau)
            )

    def _stop_step_progress_polling(self):
        if self._step_progress_task and not self._step_progress_task.done():
            self._step_progress_task.cancel()

    def _start_mode_event_drain(self):
        """Start background task that drains mode event queue every 0.3s."""
        self._stop_mode_event_drain()
        self._mode_drain_task = asyncio.create_task(self._poll_mode_events())

    def _stop_mode_event_drain(self):
        if self._mode_drain_task and not self._mode_drain_task.done():
            self._mode_drain_task.cancel()

    async def _poll_mode_events(self):
        """Periodically drain the mode event queue and send to browser.

        Runs as a background task concurrent with _stream_graph,
        so utterances/findings appear in real-time during subgraph execution.
        """
        try:
            while not self._cancelled:
                try:
                    mode_queue = get_mode_event_queue(self._session_id)
                    while not mode_queue.empty():
                        mode_event = mode_queue.get_nowait()
                        await self._send({
                            "type": mode_event.get("type", "mode_event"),
                            "ts": time.time(),
                            "data": mode_event.get("data", {}),
                        })
                except Exception as drain_err:
                    print(f"[MODE-DRAIN-ERROR] {type(drain_err).__name__}: {drain_err}")
                await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            # Final drain on cancellation — flush remaining events
            try:
                mode_queue = get_mode_event_queue(self._session_id)
                while not mode_queue.empty():
                    mode_event = mode_queue.get_nowait()
                    await self._send({
                        "type": mode_event.get("type", "mode_event"),
                        "ts": time.time(),
                        "data": mode_event.get("data", {}),
                    })
            except Exception:
                pass

    async def _poll_step_progress(self, node_name: str, label: str, tau: float):
        """Send step-level progress for long-running non-worker nodes.

        Uses the same exponential decay formula as worker progress.
        Sends 'step_progress' events so the frontend can show a progress bar
        for phases like ceo_final_report that have no per-worker tracking.
        """
        import math
        start = time.time()
        try:
            while not self._cancelled:
                elapsed = time.time() - start
                progress = min(_MAX_SIMULATED, 1.0 - math.exp(-elapsed / tau))
                await self._send({
                    "type": "step_progress",
                    "ts": time.time(),
                    "data": {
                        "node": node_name,
                        "label": label,
                        "progress": round(progress, 3),
                    },
                })
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            # Send final 100% on completion
            await self._send({
                "type": "step_progress",
                "ts": time.time(),
                "data": {"node": node_name, "label": label, "progress": 1.0},
            })
        except Exception as e:
            print(f"[DEBUG-STEP-PROGRESS] Error: {e}")

    async def _poll_worker_progress(self):
        """Send worker progress updates every second.

        Race condition guard: polling starts when ceo_work_order emits
        phase=worker_execution, but tracker.start() is only called inside
        worker_execution_node (which hasn't started yet). We wait up to
        120s for the tracker to activate before entering the main loop.

        Sends heartbeat pings every 10s during the wait phase to prevent
        WebSocket idle disconnection.
        """
        tracker = get_tracker(self._session_id)
        try:
            # Wait for tracker activation (worker_execution node calls tracker.start())
            print("[DEBUG-PROGRESS] Waiting for tracker activation...")
            waited = 0
            for _ in range(240):  # 240 × 0.5s = 120s max wait
                if tracker.is_active or self._cancelled:
                    break
                await asyncio.sleep(0.5)
                waited += 1
                # Send heartbeat every 10s to keep WebSocket alive
                if waited % 20 == 0:  # 20 × 0.5s = 10s
                    await self._send({"type": "heartbeat", "ts": time.time()})

            if self._cancelled:
                print("[DEBUG-PROGRESS] Cancelled while waiting for tracker")
                return
            if not tracker.is_active:
                print(f"[DEBUG-PROGRESS] Tracker never activated after {waited * 0.5}s!")
                return
            print(f"[DEBUG-PROGRESS] Tracker active after {waited * 0.5}s wait")

            poll_count = 0
            while tracker.is_active and not self._cancelled:
                workers, elapsed = tracker.snapshot()
                now = time.time()
                poll_count += 1
                if poll_count <= 5 or poll_count % 10 == 0:
                    statuses = {w.domain: f"{w.status.value}({round(compute_progress(w, now)*100)}%)" for w in workers}
                    print(f"[DEBUG-PROGRESS] Poll #{poll_count} elapsed={elapsed:.1f}s workers={statuses}")
                for w in workers:
                    progress = compute_progress(w, now)
                    await self._send({
                        "type": "progress",
                        "ts": now,
                        "data": {
                            "character": w.display_name,
                            "worker_id": w.worker_id or w.domain,
                            "worker_name": w.worker_name,
                            "role_type": w.role_type,
                            "progress": round(progress, 3),
                            "tier": w.tier,
                            "status": w.status.value,
                            "summary": w.summary,
                        },
                    })
                await asyncio.sleep(1.0)
            print(f"[DEBUG-PROGRESS] Poll loop ended. tracker.is_active={tracker.is_active}, cancelled={self._cancelled}")

            # Send final 100% for all workers to guarantee UI completion
            workers, _ = tracker.snapshot()
            now = time.time()
            for w in workers:
                await self._send({
                    "type": "progress",
                    "ts": now,
                    "data": {
                        "character": w.domain,
                        "worker_id": w.worker_id or w.domain,
                        "progress": 1.0,
                        "tier": w.tier,
                        "status": "done",
                        "summary": w.summary,
                    },
                })
            print(f"[DEBUG-PROGRESS] Sent 100% for {len(workers)} workers")
        except asyncio.CancelledError:
            # Also send 100% on cancel (e.g., scene_change to next node)
            try:
                workers, _ = tracker.snapshot()
                now = time.time()
                for w in workers:
                    await self._send({
                        "type": "progress",
                        "ts": now,
                        "data": {
                            "character": w.domain,
                            "worker_id": w.worker_id or w.domain,
                            "progress": 1.0,
                            "tier": w.tier,
                            "status": "done",
                            "summary": w.summary,
                        },
                    })
                print(f"[DEBUG-PROGRESS] Sent 100% on cancel for {len(workers)} workers")
            except Exception:
                pass
        except Exception as e:
            print(f"[DEBUG-PROGRESS] Poll task error: {e}")
            import traceback; traceback.print_exc()

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to keep WebSocket alive.

        Prevents idle disconnection during long-running graph operations
        where no application-level data flows (e.g., waiting for worker
        tracker activation, Claude Code subprocess execution).
        """
        try:
            while not self._cancelled:
                await asyncio.sleep(15)
                await self._send({"type": "heartbeat", "ts": time.time()})
        except asyncio.CancelledError:
            pass

    async def _listen_for_input(self):
        """Receive WebSocket messages and resolve interrupt futures."""
        while not self._cancelled:
            try:
                msg = await self.ws.receive_json()
                if msg.get("type") == "stop":
                    self.cancel()
                    break
                if msg.get("type") == "interrupt_response":
                    if self._interrupt_future and not self._interrupt_future.done():
                        self._interrupt_future.set_result(msg.get("data"))
            except Exception:
                break

    def _extract_interrupt(self, snapshot) -> dict | None:
        """Extract interrupt data from graph state snapshot."""
        for task in snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                return task.interrupts[0].value
        return None

    def _build_team_pre_context(self, team_id: str, user_id: str) -> dict:
        """Load saved team and build pre_context for direct execution."""
        from src.company_builder.storage import load_company
        company = load_company(user_id, team_id)
        if not company or not company.get("agents"):
            return {}

        agents = company["agents"]
        edges = company.get("edges", [])
        domains = list({a.get("tool_category", "research") for a in agents})

        team_lines = []
        for a in agents:
            role_type = a.get("role_type", "executor")
            team_lines.append(
                f"- {a.get('name', '?')} [{role_type}]: {a.get('role', '?')} (도구: {a.get('tool_category', '?')})"
            )

        return {
            "team_agents": agents,
            "team_edges": edges,
            "team_name": company.get("name", ""),
            "selected_domains": domains,
            "background": f"## 사용자 정의 팀: {company.get('name', '')}\n" + "\n".join(team_lines),
            "default_answer": "팀 구조에 따라 최선의 판단으로 진행하세요.",
            "escalation_policy": "auto_proceed",
        }

    def cancel(self):
        self._cancelled = True
        if self._graph_task and not self._graph_task.done():
            self._graph_task.cancel()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._stop_progress_polling()
        self._stop_step_progress_polling()
        # Kill only this session's subprocesses (safe for concurrent mode)
        if self._session_id:
            pids = get_pids_by_session(f"company_{self._session_id}")
            if pids:
                cleanup_specific_pids(pids)

    async def _send(self, data: dict):
        """Send JSON to WebSocket, ignoring errors on closed connections."""
        try:
            await self.ws.send_json(data)
        except Exception:
            self._cancelled = True
