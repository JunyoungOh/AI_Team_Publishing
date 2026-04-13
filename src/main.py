"""CLI entry point for the Enterprise Agent System."""

from __future__ import annotations

import asyncio
import time
import uuid

from dotenv import load_dotenv

load_dotenv()  # .env → os.environ (MCP servers need API keys in process env)

from src.config.settings import get_settings
if not get_settings().use_api_direct:
    from src.utils.claude_code import verify_cli
    try:
        verify_cli()
    except Exception as e:
        import sys
        print(f"[CLI Check Failed] {e}", file=sys.stderr)
        sys.exit(1)

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.tree import Tree
from src.engine import ResumeCommand

from src.utils.progress import (
    WorkerStatus, WorkerProgress, compute_progress, get_tracker,
    StepProgress, StepStatus, compute_step_progress, get_step_tracker,
    NODE_LABELS,
)
from rich.console import Group

from pathlib import Path

from src.engine import SqliteCheckpointer

from src.config.settings import get_settings
from src.graphs.main_graph import build_pipeline
from src.models.state import create_initial_state
from src.utils.execution_tracker import reset_exec_tracker
from src.utils.logging import setup_logging
from src.utils.tracing import configure_tracing, is_tracing_active, get_run_config

console = Console()

# ── Node display labels (from progress.py shared dict) ───

# Nodes that execute tasks in parallel (kept for UI tracker API compatibility).
# 싱글 세션 모드에서는 병렬 노드 개념이 없음 — 빈 세트.
_PARALLEL_NODES: set[str] = set()

# Mutable state for tracking UI across stream calls
_ui_state: dict = {"last_node": None, "step": 0}


# ── Main entry ────────────────────────────────────────────


async def run(resume_id: str | None = None) -> None:
    """Run the enterprise agent system interactively.

    Args:
        resume_id: Optional session ID to resume from a checkpoint.
    """
    setup_logging()
    tracing = configure_tracing()

    db_path = get_settings().checkpoint_db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    async with SqliteCheckpointer(db_path) as checkpointer:
        await _run_session(checkpointer, tracing, resume_id)

        # After first task, loop for additional tasks (interactive mode only)
        if not resume_id:
            while True:
                console.print()
                choice = Prompt.ask(
                    "[bold]새 작업을 입력하거나, 종료하려면 'q'를 입력하세요[/bold]",
                    default="q",
                )
                if choice.strip().lower() in ("q", "quit", "exit", "종료"):
                    console.print("[dim]종료합니다.[/dim]")
                    break
                if not choice.strip():
                    continue
                await _run_session(checkpointer, tracing, task_override=choice.strip())


async def _run_session(
    checkpointer,
    tracing: bool,
    resume_id: str | None = None,
    task_override: str | None = None,
) -> None:
    """Inner session logic, called within the checkpointer context."""
    app = build_pipeline(checkpointer=checkpointer)

    if resume_id:
        thread_id = resume_id
    else:
        thread_id = str(uuid.uuid4())[:8]

    config = get_run_config(
        thread_id,
        mode="resume" if resume_id else "interactive",
    )

    # Welcome header
    mode_text = "세션 재개" if resume_id else "CEO-Leader-Worker 기업형 에이전트"
    tracing_text = " | [green]LangSmith[/green]" if tracing else ""
    console.print(Panel(
        "[bold]Enterprise Agent System[/bold]\n"
        f"[dim]{mode_text} | session: {thread_id}{tracing_text}[/dim]",
        style="blue",
    ))

    start_time = time.time()
    _ui_state["last_node"] = None
    _ui_state["step"] = 0
    get_step_tracker().start_pipeline()
    reset_exec_tracker()  # Fresh metrics for each session

    # Load domain plugins (YAML-based domain registration)
    settings = get_settings()
    if settings.enable_plugins:
        from src.config.plugin_loader import load_and_merge_plugins
        plugin_results = load_and_merge_plugins(settings.plugin_dir)
        if plugin_results:
            loaded = [d for d, s in plugin_results.items() if s == "registered"]
            if loaded:
                console.print(f"[dim]플러그인 도메인 로드: {', '.join(loaded)}[/dim]")

    try:
        if resume_id:
            # ── Resume mode: check existing session state ──
            snapshot = await app.aget_state(config)
            if not snapshot:
                console.print("[yellow]세션을 찾을 수 없습니다. 새 세션을 시작하세요.[/yellow]")
                return
            if not snapshot.next:
                console.print("[dim]이 세션은 이미 완료되었습니다.[/dim]")
                return

            interrupt_data = None
            for task in snapshot.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    interrupt_data = task.interrupts[0].value
                    break

            if not interrupt_data:
                console.print(
                    "[yellow]대기 중인 인터럽트가 없습니다. "
                    "새 세션을 시작하세요.[/yellow]"
                )
                return

            console.print()
            console.rule("[dim]세션 재개[/dim]")
            console.print()

            user_response = _handle_interrupt(interrupt_data)

            console.print()
            console.rule("[dim]재개[/dim]")
            console.print()
            await _stream_with_status(app, ResumeCommand(value=user_response), config)

        else:
            # ── New session mode ──
            if task_override:
                user_task = task_override
            else:
                user_task = Prompt.ask("\n[bold]작업을 지시해주세요[/bold]")

            if not user_task.strip():
                console.print("[red]작업 지시가 비어있습니다.[/red]")
                return

            initial = create_initial_state(user_task, session_id=thread_id)

            console.print()
            console.rule("[dim]작업 시작[/dim]")
            console.print()
            await _stream_with_status(app, initial, config)

        # Handle subsequent interrupts (shared for both modes)
        while True:
            snapshot = await app.aget_state(config)
            if not snapshot or not snapshot.next:
                break

            interrupt_data = None
            for task in snapshot.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    interrupt_data = task.interrupts[0].value
                    break

            if interrupt_data is None:
                break

            user_response = _handle_interrupt(interrupt_data)

            console.print()
            console.rule("[dim]재개[/dim]")
            console.print()
            await _stream_with_status(app, ResumeCommand(value=user_response), config)

    except KeyboardInterrupt:
        from src.utils.claude_code import cleanup_all_subprocesses
        cleanup_all_subprocesses(grace_period=3.0)
        console.print("\n[bold yellow]작업이 사용자에 의해 중단되었습니다.[/bold yellow]")
        console.print(f"[dim]재개하려면: enterprise-agent --resume {thread_id}[/dim]")
        return

    elapsed = time.time() - start_time

    # Show report folder path if generated
    try:
        snapshot = await app.aget_state(config)
        report_path = snapshot.values.get("report_file_path", "")
        if report_path:
            console.print(f"\n   [bold green]Reports saved:[/bold green] {report_path}/")
            console.print(f"     [dim]results.html  — Analysis findings[/dim]")
            quality_file = Path(report_path) / "quality.html"
            if quality_file.exists():
                console.print(f"     [dim]quality.html  — Quality assessment[/dim]")

        # Show execution metrics if available
        metrics = snapshot.values.get("execution_metrics", {})
        if metrics and metrics.get("worker_count", 0) > 0:
            _print_metrics_panel(metrics)
    except Exception:
        pass

    console.print()
    console.rule(f"[dim]작업 완료 ({elapsed:.1f}s)[/dim]")


# ── Streaming with spinner ────────────────────────────────


# ── Progress bar constants ────────────────────────────────

_BAR_WIDTH = 24
_BAR_FILL = "\u2588"   # █
_BAR_EMPTY = "\u2591"  # ░

_BAR_COLORS = {
    WorkerStatus.PENDING: "dim",
    WorkerStatus.WAITING: "cyan",
    WorkerStatus.RUNNING: "yellow",
    WorkerStatus.TIER2: "magenta",
    WorkerStatus.DONE: "green",
    WorkerStatus.FAILED: "red",
}

_BAR_ICONS = {
    WorkerStatus.PENDING: "\u23f3",  # ⏳
    WorkerStatus.WAITING: "\u23f3",  # ⏳ (waiting for dependency)
    WorkerStatus.RUNNING: "\u26a1",  # ⚡
    WorkerStatus.TIER2: "\U0001f504",  # 🔄
    WorkerStatus.DONE: "\u2705",     # ✅
    WorkerStatus.FAILED: "\u274c",   # ❌
}


def _format_elapsed(seconds: float) -> str:
    """Format seconds into a human-readable elapsed string."""
    if seconds <= 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{mins}m {secs:02d}s"


_STEP_BAR_WIDTH = 20


def _render_step_bar(progress: float) -> str:
    """Render a simple yellow progress bar for pipeline steps."""
    filled = int(progress * _STEP_BAR_WIDTH)
    empty = _STEP_BAR_WIDTH - filled
    pct = int(progress * 100)
    bar_filled = f"[yellow]{_BAR_FILL * filled}[/yellow]"
    bar_empty = f"[dim]{_BAR_EMPTY * empty}[/dim]"
    return f"{bar_filled}{bar_empty} [yellow]{pct:3d}%[/yellow]"


def _build_pipeline_panel(
    steps: list[StepProgress],
    pipeline_elapsed: float,
    worker_snapshot: tuple[list[WorkerProgress], float] | None = None,
) -> Panel:
    """Build a Rich Panel showing pipeline progress for all steps."""
    lines: list[str] = []
    now = time.time()

    for step in steps:
        num = step.step_number
        label = step.label

        if step.status == StepStatus.DONE:
            elapsed = step.finished_at - step.started_at if step.finished_at > 0 else 0
            lines.append(f"  [green]\u2705 {num}. {label}[/green] [dim]({_format_elapsed(elapsed)})[/dim]")

        elif step.node_name == "await_user_answers":
            lines.append(f"  [cyan]\u23f3 {num}. {label}[/cyan] [dim](사용자 입력 대기)[/dim]")

        elif step.node_name == "worker_execution" and worker_snapshot:
            # Step header with elapsed time
            elapsed = now - step.started_at if step.started_at > 0 else 0
            lines.append(f"  [yellow]\u26a1 {num}. {label}[/yellow]  [dim]{_format_elapsed(elapsed)}[/dim]")
            # Inline worker progress rows
            workers, _ = worker_snapshot
            for w in workers:
                progress = compute_progress(w, now)
                bar = _render_bar(progress, w.status)
                if w.finished_at > 0 and w.started_at > 0:
                    w_elapsed = w.finished_at - w.started_at
                elif w.started_at > 0:
                    w_elapsed = now - w.started_at
                else:
                    w_elapsed = 0
                lines.append(f"    [cyan]{w.domain:<16}[/cyan] {bar}  [dim]{_format_elapsed(w_elapsed)}[/dim]")

        else:
            # Running step with animated progress bar
            progress = compute_step_progress(step, now)
            if progress > 0:
                bar = _render_step_bar(progress)
                elapsed = now - step.started_at if step.started_at > 0 else 0
                lines.append(f"  [yellow]\u26a1 {num}. {label}[/yellow]  {bar}  [dim]{_format_elapsed(elapsed)}[/dim]")
            else:
                lines.append(f"  [yellow]\u26a1 {num}. {label}[/yellow]  [dim]시작 중...[/dim]")

    content = "\n".join(lines) if lines else "[dim]파이프라인 시작 대기 중...[/dim]"
    return Panel(
        content,
        title=f"[bold magenta]Pipeline Progress[/bold magenta] [dim]({_format_elapsed(pipeline_elapsed)})[/dim]",
        border_style="magenta",
        expand=False,
        padding=(0, 1),
    )


async def _render_pipeline_loop(live: Live) -> None:
    """Continuously render the pipeline panel inside an active Live context."""
    step_tracker = get_step_tracker()
    worker_tracker = get_tracker()

    try:
        while True:
            steps, pipeline_elapsed = step_tracker.snapshot()
            # Include worker dashboard if worker tracker is active
            worker_snap = worker_tracker.snapshot() if worker_tracker.is_active else None
            panel = _build_pipeline_panel(steps, pipeline_elapsed, worker_snap)
            live.update(panel)
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        # Final render
        steps, pipeline_elapsed = step_tracker.snapshot()
        worker_snap = worker_tracker.snapshot() if worker_tracker.is_active else None
        panel = _build_pipeline_panel(steps, pipeline_elapsed, worker_snap)
        live.update(panel)


def _render_bar(progress: float, status: WorkerStatus) -> str:
    """Render a colored progress bar: ████████░░░░░░ 67% ⚡"""
    filled = int(progress * _BAR_WIDTH)
    empty = _BAR_WIDTH - filled
    pct = int(progress * 100)

    color = _BAR_COLORS[status]
    icon = _BAR_ICONS[status]

    bar_filled = f"[{color}]{_BAR_FILL * filled}[/{color}]"
    bar_empty = f"[dim]{_BAR_EMPTY * empty}[/dim]"
    label = f"[{color}]{pct:3d}%[/{color}] {icon}"

    return f"{bar_filled}{bar_empty} {label}"


def _build_dashboard_table(workers: list[WorkerProgress], elapsed: float) -> Table:
    """Build a Rich Table with animated progress bars per worker."""
    table = Table(
        title="[bold magenta]Worker Execution Dashboard[/bold magenta]",
        border_style="magenta",
        show_lines=False,
        expand=False,
        padding=(0, 1),
    )
    table.add_column("Domain", style="cyan", no_wrap=True, min_width=20)
    table.add_column("Progress", no_wrap=True, min_width=36)
    table.add_column("Time", justify="right", style="dim", min_width=7)

    done_count = 0
    now = time.time()

    for w in workers:
        progress = compute_progress(w, now)
        bar = _render_bar(progress, w.status)

        if w.finished_at > 0 and w.started_at > 0:
            worker_elapsed = w.finished_at - w.started_at
        elif w.started_at > 0:
            worker_elapsed = now - w.started_at
        else:
            worker_elapsed = 0

        if w.status in (WorkerStatus.DONE, WorkerStatus.FAILED):
            done_count += 1

        table.add_row(w.domain, bar, _format_elapsed(worker_elapsed))

    table.caption = (
        f"[dim]{done_count}/{len(workers)} complete | "
        f"Total: {_format_elapsed(elapsed)}[/dim]"
    )
    return table


async def _stream_with_status(app, input_data, config) -> None:
    """Stream graph events with unified pipeline progress panel."""
    step_tracker = get_step_tracker()
    render_task: asyncio.Task | None = None

    with Live(console=console, refresh_per_second=2, transient=False) as live:
        render_task = asyncio.create_task(_render_pipeline_loop(live))

        try:
            async for event in app.astream(
                input_data, config=config,
            ):
                for node_name, update in event.items():
                    # Skip non-dict updates (e.g. __interrupt__ sends tuples)
                    if not isinstance(update, dict):
                        continue

                    # Worker execution completed — stop worker tracker
                    if node_name == "worker_execution":
                        get_tracker().stop()

                    # Show node transition header with step counter
                    if node_name != _ui_state["last_node"]:
                        _ui_state["step"] += 1
                        step = _ui_state["step"]
                        label = NODE_LABELS.get(node_name, node_name)
                        # Note: begin_step() is called by node_error_handler decorator
                        # at node START (not here at event arrival) for real-time progress
                        console.print(f"\n[bold cyan]Step {step} >> {label}[/bold cyan]")
                        _ui_state["last_node"] = node_name

                    messages = update.get("messages", [])

                    # Show parallel execution indicator
                    if len(messages) > 1 and node_name in _PARALLEL_NODES:
                        console.print(f"   [dim]({len(messages)}건 병렬 처리)[/dim]")

                    # Show hierarchy tree after worker assembly
                    if node_name == "assemble_workers" and "workers" in update:
                        _print_hierarchy_from_workers(update["workers"])

                    # Print messages with role-based styling
                    for msg in messages:
                        content = msg.content if hasattr(msg, "content") else str(msg)
                        _print_message(content, node_name)

                    # Detect work order → leader task decomposition transition
                    phase = update.get("phase", "")
                    if phase == "leader_task_decomposition" and node_name == "ceo_work_order":
                        console.print(
                            "\n   [bold magenta]>>> 작업지시서 완성 — "
                            "리더 작업 분해 단계 진입[/bold magenta]\n"
                        )


        finally:
            step_tracker.finish_current()
            get_tracker().stop()
            if render_task:
                render_task.cancel()
                try:
                    await render_task
                except (asyncio.CancelledError, Exception):
                    pass


# ── Hierarchy display ─────────────────────────────────────


def _print_hierarchy(leaders: list[dict]) -> None:
    """Display CEO-Leader-Worker hierarchy as a tree (legacy)."""
    tree = Tree("[bold blue]CEO[/bold blue]")
    for leader in leaders:
        domain = leader.get("leader_domain", "unknown")
        workers = leader.get("workers", [])
        branch = tree.add(
            f"[bold green]{domain} leader[/bold green] "
            f"[dim]({len(workers)}명)[/dim]"
        )
        for worker in workers:
            w_domain = worker.get("worker_domain", "unknown")
            branch.add(f"[yellow]{w_domain}[/yellow]")
    console.print(tree)


def _print_hierarchy_from_workers(workers: list[dict]) -> None:
    """Display CEO-Worker hierarchy as a tree (2-tier architecture)."""
    tree = Tree("[bold blue]CEO[/bold blue]")
    for worker in workers:
        w_domain = worker.get("worker_domain", "unknown")
        tree.add(f"[yellow]{w_domain}[/yellow]")
    console.print(tree)


# ── Message formatting ────────────────────────────────────


def _print_message(content: str, node_name: str) -> None:
    """Print a message with role-appropriate styling."""
    # CEO Final Report — rich formatted output
    if content.startswith("[CEO Final Report]"):
        _print_final_report(content)
        return

    # Error terminal — always red
    if node_name == "error_terminal":
        console.print(f"   [bold red]{content}[/bold red]")
        return

    # System messages — red
    if content.startswith("[System]") or content.startswith("[system]"):
        console.print(f"   [red]{content}[/red]")
        return

    # Failure/error messages — red
    if " failed" in content or "Error:" in content:
        console.print(f"   [bold red]{content}[/bold red]")
        return

    # Generated Files — show as Rich panel
    if content.startswith("[Generated Files]"):
        file_lines = content.split("\n")[1:]  # Skip the header line
        if file_lines:
            console.print(Panel(
                "\n".join(file_lines),
                title="[bold cyan]생성된 파일[/bold cyan]",
                border_style="cyan",
            ))
        return

    # Report saved messages — green
    if content.startswith("[Report"):
        console.print(f"   [bold green]{content}[/bold green]")
        return

    # CEO messages — blue
    if content.startswith("[CEO"):
        console.print(f"   [bold blue]{content}[/bold blue]")
        return

    # Leader messages — match "[domain leader]" and "[domain leader -> ...]" patterns
    if " leader]" in content or " leader ->" in content:
        console.print(f"   [bold green]{content}[/bold green]")
        return

    # Worker / default — yellow
    console.print(f"   [yellow]{content}[/yellow]")


# ── Final report formatting ───────────────────────────────


def _print_final_report(content: str) -> None:
    """Format CEO final report with Rich Panel and Table."""
    sections = _parse_report_sections(content)

    console.print()

    # Summary
    summary = sections.get("Summary", "").strip()
    if summary:
        console.print(Panel(
            summary,
            title="[bold blue]Summary[/bold blue]",
            border_style="blue",
        ))

    # Domain Results as table
    domain_text = sections.get("Domain Results", "").strip()
    if domain_text:
        table = Table(title="Domain Results", border_style="cyan", show_lines=True)
        table.add_column("Domain", style="cyan", no_wrap=True)
        table.add_column("Summary")
        table.add_column("Quality", justify="center", style="bold")
        table.add_column("Gaps", style="yellow")

        for line in domain_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = _parse_domain_line(line)
            quality = parsed.get("quality", "")
            # Color quality score by value
            try:
                score = int(quality.split("/")[0])
                q_style = "green" if score >= 7 else "yellow" if score >= 5 else "red"
                quality = f"[{q_style}]{quality}[/{q_style}]"
            except (ValueError, IndexError):
                pass
            table.add_row(
                parsed.get("domain", ""),
                parsed.get("summary", line),
                quality,
                parsed.get("gaps", ""),
            )
        console.print(table)

    # Gap Analysis
    gap = sections.get("Gap Analysis", "").strip()
    if gap:
        console.print(Panel(
            gap,
            title="[bold yellow]Gap Analysis[/bold yellow]",
            border_style="yellow",
        ))

    # Recommendations
    recs = sections.get("Recommendations", "").strip()
    if recs:
        console.print(Panel(
            recs,
            title="[bold green]Recommendations[/bold green]",
            border_style="green",
        ))


def _parse_report_sections(content: str) -> dict[str, str]:
    """Parse report content into sections by ## headers."""
    sections: dict[str, str] = {}
    current = "_header"
    lines: list[str] = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if lines:
                sections[current] = "\n".join(lines)
            current = line[3:].strip()
            lines = []
        else:
            lines.append(line)

    if lines:
        sections[current] = "\n".join(lines)
    return sections


def _parse_domain_line(line: str) -> dict[str, str]:
    """Parse '[domain] summary - quality: N/10 (gaps: ...)' format."""
    result: dict[str, str] = {}
    line = line.strip()

    if line.startswith("[") and "]" in line:
        end = line.index("]")
        result["domain"] = line[1:end]
        rest = line[end + 1:].strip()
    else:
        rest = line

    if "quality:" in rest:
        before, after = rest.split("quality:", 1)
        result["summary"] = before.rstrip(" -").strip()
        after = after.strip()
        if "(" in after:
            score_part, remainder = after.split("(", 1)
            result["quality"] = score_part.strip()
            if "gaps:" in remainder:
                gaps = remainder.split("gaps:", 1)[1].rstrip(")").strip()
                result["gaps"] = gaps
        else:
            result["quality"] = after.strip()
    else:
        result["summary"] = rest

    return result


# ── Interrupt handling ────────────────────────────────────


def _handle_interrupt(interrupt_data: dict) -> dict | str:
    """Handle a human-in-the-loop interrupt."""
    import sys
    import termios

    # Restore terminal state after Rich Live display.
    # Live may leave stdin echo/canonical mode disabled on macOS.
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] |= termios.ECHO | termios.ICANON
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (termios.error, ValueError, OSError):
        pass

    interrupt_type = interrupt_data.get("type", "")

    if interrupt_type == "clarifying_questions":
        console.print()
        console.print(Panel(
            "[bold]리더들의 질문에 답변해주세요[/bold]",
            style="cyan",
        ))
        questions = interrupt_data.get("questions", {})
        answers = {}

        for domain, q_list in questions.items():
            console.print(f"\n[bold cyan]-- {domain} 리더 질문 --[/bold cyan]")
            domain_answers = []
            for i, q in enumerate(q_list, 1):
                console.print(f"  {i}. {q}")
                answer = Prompt.ask(f"    답변 {i}")
                domain_answers.append(answer)
            answers[domain] = domain_answers

        return answers

    elif interrupt_type == "result_review":
        summary = interrupt_data.get("summary", "")
        instructions = interrupt_data.get("instructions", "")

        console.print()
        console.print(Panel(
            "[bold]워커 실행 결과 리뷰[/bold]",
            style="cyan",
        ))
        if summary:
            console.print(Panel(summary, title="실행 결과 요약", border_style="dim"))
        if instructions:
            console.print(f"  [dim]{instructions}[/dim]")
        console.print()
        console.print("  [bold]1.[/bold] 확인 (Gap analysis 진행)")
        console.print("  [bold]2.[/bold] 수정 요청 (피드백 입력)")
        console.print("  [bold]3.[/bold] 중단 (현재 결과로 보고서 생성)")
        choice = Prompt.ask("선택", choices=["1", "2", "3"], default="1")
        if choice == "1":
            return {"action": "confirm"}
        elif choice == "3":
            return {"action": "abort"}
        feedback = Prompt.ask("수정 피드백을 입력하세요")
        return {"action": "revise", "feedback": feedback}

    elif interrupt_type == "escalation" or interrupt_data.get("escalation_reason"):
        console.print()
        console.print(Panel(
            f"[bold]에스컬레이션[/bold]\n\n{interrupt_data.get('message', '')}",
            style="red",
        ))
        options = interrupt_data.get("options", [])
        for i, opt in enumerate(options, 1):
            console.print(f"  {i}. {opt}")
        choice = Prompt.ask("\n[bold]선택 (번호 또는 직접 입력)[/bold]")
        try:
            idx = int(choice) - 1
            return options[idx] if 0 <= idx < len(options) else choice
        except ValueError:
            return choice

    else:
        return Prompt.ask("입력")


# ── Execution metrics display ────────────────────────


def _print_metrics_panel(metrics: dict) -> None:
    """Display execution metrics as a Rich Panel after task completion."""
    lines: list[str] = []

    total_s = metrics.get("total_session_seconds", 0)
    worker_count = metrics.get("worker_count", 0)
    avg_s = metrics.get("avg_worker_duration_s", 0)
    max_s = metrics.get("max_worker_duration_s", 0)
    tiers = metrics.get("tier_distribution", {})
    cache = metrics.get("cache_stats", {})

    lines.append(f"  총 소요 시간:          {_format_elapsed(total_s)}")
    lines.append(f"  워커 수:               {worker_count}")
    lines.append(f"  평균 워커 실행 시간:   {_format_elapsed(avg_s)}")

    # Find slowest worker
    workers = metrics.get("workers", [])
    if workers:
        slowest = max(workers, key=lambda w: w.get("duration_s", 0))
        lines.append(
            f"  최대 워커 실행 시간:   {_format_elapsed(slowest['duration_s'])} "
            f"({slowest['domain']})"
        )

    # Tier distribution
    tier_parts = []
    for t in sorted(tiers.keys()):
        label = t.replace("tier_", "T")
        if t == "tier_0":
            label = "Cache"
        tier_parts.append(f"{label}: {tiers[t]}")
    if tier_parts:
        lines.append(f"  Tier 분포:             {', '.join(tier_parts)}")

    # Cache stats
    if cache:
        hit_rate = cache.get("hit_rate_pct", 0)
        lines.append(f"  캐시 히트율:           {hit_rate}%")

    # Node breakdown
    nodes = metrics.get("nodes", [])
    if nodes:
        lines.append("")
        lines.append("  [dim]노드별 소요시간[/dim]")
        for n in nodes:
            name = n["node"]
            dur = n["duration_s"]
            if dur > 0:
                label = NODE_LABELS.get(name, name)
                lines.append(f"    {label:<20} {_format_elapsed(dur)}")

    content = "\n".join(lines)
    console.print()
    console.print(Panel(
        content,
        title="[bold magenta]Execution Metrics[/bold magenta]",
        border_style="magenta",
        expand=False,
        padding=(0, 1),
    ))


# ── CLI entry point ───────────────────────────────────────


def _interactive_signal_handler(signum, frame):
    """Handle SIGTERM in interactive mode — clean up subprocesses before exit."""
    from src.utils.claude_code import cleanup_all_subprocesses
    cleanup_all_subprocesses(grace_period=3.0)
    raise SystemExit(1)


def main():
    import signal
    signal.signal(signal.SIGTERM, _interactive_signal_handler)

    import argparse

    parser = argparse.ArgumentParser(
        prog="enterprise-agent",
        description="Enterprise Agent System -- CEO-Leader-Worker 기업형 에이전트",
    )
    parser.add_argument(
        "--resume", metavar="SESSION_ID",
        help="Resume a previous session by thread ID",
    )
    parser.add_argument(
        "--ui", choices=["tui", "sim"], default="tui",
        help="UI mode: 'tui' (terminal, default) or 'sim' (browser simulation)",
    )
    subparsers = parser.add_subparsers(dest="command")

    from src.scheduler.cli import build_schedule_parser
    build_schedule_parser(subparsers)

    args = parser.parse_args()

    if args.command == "schedule":
        from src.scheduler.cli import handle_schedule_command
        handle_schedule_command(args)
    elif args.ui == "sim":
        try:
            from src.ui.server import start_sim_server
        except ImportError:
            console.print(
                "[red]시뮬레이션 UI 의존성이 설치되지 않았습니다.[/red]\n"
                "[dim]pip install -e '.[ui]' 로 설치하세요.[/dim]"
            )
            raise SystemExit(1)
        start_sim_server()
    else:
        asyncio.run(run(resume_id=getattr(args, "resume", None)))


if __name__ == "__main__":
    main()
