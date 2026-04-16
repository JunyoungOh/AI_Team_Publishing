"""Claude Code Headless / Agent SDK bridge.

Provides :class:`ClaudeCodeBridge` — an abstraction layer that invokes
Claude Code in headless mode, either via the official ``claude-code-sdk``
Python package (preferred) or by falling back to a ``subprocess`` call.

Nodes never import this directly; they go through :class:`BaseAgent._query`.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shutil
import signal
import subprocess as _subprocess_mod
import sys
import threading
import time as _time_mod
from enum import Enum
from typing import Any

from pydantic import BaseModel

from src.utils.logging import get_logger
from src.utils.tracing import traceable_llm

logger = get_logger(agent_id="claude-code-bridge")

# Lock for CLAUDECODE env-var manipulation in concurrent SDK calls
_env_lock = threading.Lock()


# ── Error classification (B1) ────────────────────────


class ErrorCategory(Enum):
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    AUTH_ERROR = "auth_error"
    TIMEOUT = "timeout"
    CLI_ERROR = "cli_error"
    MAX_TURNS = "max_turns"
    UNKNOWN = "unknown"


_ERROR_PATTERNS: dict[ErrorCategory, list[str]] = {
    ErrorCategory.RATE_LIMIT: ["rate limit", "rate_limit", "too many requests", "429"],
    ErrorCategory.OVERLOADED: ["overloaded", "529", "capacity"],
    ErrorCategory.SERVER_ERROR: ["internal server error", "500", "server error"],
    ErrorCategory.AUTH_ERROR: [
        "unauthorized", "forbidden", "401", "403", "invalid api key", "invalid_api_key",
    ],
    ErrorCategory.CLI_ERROR: ["unknown flag", "invalid option", "missing required"],
    ErrorCategory.MAX_TURNS: ["max_turns", "max_turns limit"],
}


def classify_error(stderr_or_msg: str, exit_code: int = -1) -> ErrorCategory:
    """Classify an error based on stderr text and exit code."""
    text = stderr_or_msg.lower()
    for category, patterns in _ERROR_PATTERNS.items():
        if any(p in text for p in patterns):
            return category
    if exit_code == 1:
        return ErrorCategory.CLI_ERROR
    return ErrorCategory.UNKNOWN


# ── Output sanitizer (A2) ────────────────────────────

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BOM = "\ufeff"
# Matches ★ Insight blocks injected by output-style plugins
_INSIGHT_RE = re.compile(
    r'`?★\s*Insight[─\s]*`?\s*'     # opening marker
    r'.*?'                            # content (lazy)
    r'`?[─]{10,}`?',                  # closing marker
    re.DOTALL,
)


def _strip_insight_blocks(text: str) -> str:
    """Remove ★ Insight blocks from text (output-style plugin contamination)."""
    return _INSIGHT_RE.sub('', text).strip()


# Matches just the opening sentinel — used by the streaming filter to detect
# when an insight block is *about* to start (before the closing marker arrives).
_INSIGHT_START_RE = re.compile(r'`?★\s*Insight', re.DOTALL)


class InsightStreamFilter:
    """Stateful filter that strips ★ Insight blocks from a streaming text feed.

    The full-block regex (`_INSIGHT_RE`) only matches when both opening and
    closing markers are present in the buffer. For a live delta stream, a
    block can arrive across multiple chunks, so we buffer partial content
    until we know what is safe to emit.

    Contract:
        • ``feed(chunk)`` returns the portion safe to emit right now (may be "").
        • ``flush()`` drains any leftover text at end-of-stream; content inside
          an unclosed insight block is discarded.
    """

    def __init__(self) -> None:
        self._buf: str = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        # Strip any fully-formed insight blocks that are now in view.
        self._buf = _INSIGHT_RE.sub("", self._buf)
        # If an opener has appeared but no closer yet, hold back from '★'.
        start_match = _INSIGHT_START_RE.search(self._buf)
        if start_match is not None:
            safe = self._buf[: start_match.start()]
            self._buf = self._buf[start_match.start():]
            return safe
        # Also guard against a partial opener at the tail ('★ Insi' style).
        tail_star = self._buf.rfind("★")
        if tail_star >= 0 and len(self._buf) - tail_star < 20:
            safe = self._buf[:tail_star]
            self._buf = self._buf[tail_star:]
            return safe
        safe = self._buf
        self._buf = ""
        return safe

    def flush(self) -> str:
        """Return any leftover buffered text; discard if still inside a block."""
        if _INSIGHT_START_RE.search(self._buf):
            self._buf = ""
            return ""
        remaining = self._buf
        self._buf = ""
        return remaining


def _clean_insight_from_dict(data: Any) -> Any:
    """Recursively strip ★ Insight blocks from all string values in a dict."""
    if isinstance(data, str):
        return _strip_insight_blocks(data)
    if isinstance(data, dict):
        return {k: _clean_insight_from_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_clean_insight_from_dict(item) for item in data]
    return data


def _sanitize_json_output(raw: str) -> str:
    """Extract JSON object from stdout, stripping BOM/ANSI/warnings."""
    cleaned = _ANSI_RE.sub("", raw.replace(_BOM, ""))
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return cleaned[first_brace:last_brace + 1]
    return cleaned


# ── CLI health check (A1) ────────────────────────────

_CLI_MIN_VERSION = "2.1.60"
_cli_cache: dict[str, Any] | None = None


def verify_cli() -> dict[str, Any]:
    """Verify claude CLI binary exists and meets minimum version.

    Returns:
        {"ok": True, "version": "2.3.1", "path": "/usr/local/bin/claude"}
    Raises:
        ClaudeCodeError on missing CLI or version below minimum.
    """
    global _cli_cache
    if _cli_cache is not None:
        return _cli_cache

    path = shutil.which("claude")
    if not path:
        raise ClaudeCodeError(
            "claude CLI not found in PATH. "
            "Install: npm install -g @anthropic-ai/claude-code"
        )

    try:
        proc = _subprocess_mod.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        version_str = proc.stdout.strip()
    except Exception:
        version_str = ""

    match = re.search(r"(\d+\.\d+\.\d+)", version_str)
    version = match.group(1) if match else "unknown"

    if version != "unknown":
        def _ver(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split("."))
        if _ver(version) < _ver(_CLI_MIN_VERSION):
            raise ClaudeCodeError(
                f"claude CLI {version} < minimum {_CLI_MIN_VERSION}. "
                f"Update: npm update -g @anthropic-ai/claude-code"
            )

    _cli_cache = {"ok": True, "version": version, "path": path}
    logger.info("cli_verified", version=version, path=path)
    return _cli_cache


# ── Subprocess metrics (D2) ──────────────────────────


class SubprocessMetrics:
    """Thread-safe call metrics counter (singleton)."""

    _instance: SubprocessMetrics | None = None
    _create_lock = threading.Lock()

    def __new__(cls) -> SubprocessMetrics:
        if cls._instance is None:
            with cls._create_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._total = 0
                    inst._successes = 0
                    inst._failures = 0
                    inst._timeouts = 0
                    inst._retries = 0
                    inst._partial_recoveries = 0
                    inst._timeout_domains: dict[str, int] = {}
                    inst._total_elapsed = 0.0
                    inst._lock = threading.Lock()
                    cls._instance = inst
        return cls._instance

    def record(self, elapsed: float, *, success: bool, timeout: bool = False,
               retried: bool = False, domain: str = "") -> None:
        with self._lock:
            self._total += 1
            self._total_elapsed += elapsed
            if success:
                self._successes += 1
            elif timeout:
                self._timeouts += 1
                if domain:
                    self._timeout_domains[domain] = self._timeout_domains.get(domain, 0) + 1
            else:
                self._failures += 1
            if retried:
                self._retries += 1

    def record_partial_recovery(self) -> None:
        with self._lock:
            self._partial_recoveries += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = max(1, self._total)
            return {
                "total": self._total,
                "success_rate": round(self._successes / total * 100, 1),
                "timeout_rate": round(self._timeouts / total * 100, 1),
                "retry_rate": round(self._retries / total * 100, 1),
                "avg_elapsed_s": round(self._total_elapsed / total, 1),
                "partial_recoveries": self._partial_recoveries,
                "timeout_by_domain": dict(self._timeout_domains),
            }


_metrics = SubprocessMetrics()


# ── Circuit breaker (B3) ─────────────────────────────


class _CircuitBreaker:
    """Fast-fail on consecutive failures (half-open pattern)."""

    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._last_failure_time = 0.0
        self._state = "closed"  # closed / open / half-open
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = "closed"

    def record_failure(self) -> None:
        from src.config.settings import get_settings
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = _time_mod.monotonic()
            if self._consecutive_failures >= get_settings().circuit_breaker_threshold:
                self._state = "open"
                logger.warning(
                    "circuit_breaker_opened",
                    consecutive_failures=self._consecutive_failures,
                )

    def can_proceed(self) -> bool:
        from src.config.settings import get_settings
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                elapsed = _time_mod.monotonic() - self._last_failure_time
                if elapsed >= get_settings().circuit_breaker_cooldown:
                    self._state = "half-open"
                    return True
                return False
            return True  # half-open: allow one attempt


# ── Partial result recovery (B4) ─────────────────────


def _try_partial_recovery(raw: str, output_schema: type[BaseModel]) -> BaseModel | None:
    """Attempt to recover a valid result from malformed JSON output."""
    # Strategy 1: extract "result" field via regex
    m = re.search(r'"result"\s*:\s*(\{[^}]+\})', raw)
    if m:
        try:
            return output_schema.model_validate(json.loads(m.group(1)))
        except Exception:
            pass

    # Strategy 2: find last complete JSON object
    for i in range(len(raw) - 1, -1, -1):
        if raw[i] == "}":
            depth = 0
            for j in range(i, -1, -1):
                if raw[j] == "}":
                    depth += 1
                elif raw[j] == "{":
                    depth -= 1
                if depth == 0:
                    try:
                        return output_schema.model_validate(json.loads(raw[j:i + 1]))
                    except Exception:
                        break
            break

    return None


# ── Stream-JSON parsing ──────────────────────────────


def _extract_all_assistant_text(stream_output: str) -> str:
    """Parse ``--output-format stream-json`` NDJSON and return all assistant text.

    Claude Code ``stream-json`` emits one JSON object per line.  Assistant
    messages have ``{"type": "assistant", "message": {"content": [...]}}``.
    We concatenate the ``text`` blocks from every assistant event so that
    multi-turn HTML output is not truncated.

    Fallback order:
      1. Concatenated assistant text blocks (preferred — captures all turns)
      2. ``result`` field from the final ``{"type": "result"}`` event
      3. Raw stdout as-is (safety net if format is unexpected)
    """
    texts: list[str] = []
    result_text = ""

    for line in stream_output.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        if event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])

        elif event_type == "result":
            if event.get("is_error"):
                raise ClaudeCodeError(event.get("result", "Unknown error"))
            if event.get("subtype") == "error_max_turns":
                logger.warning(
                    "raw_query_max_turns",
                    num_turns=event.get("num_turns"),
                )
            result_text = event.get("result", "")

    if texts:
        return _strip_insight_blocks("\n".join(texts))
    if result_text:
        return _strip_insight_blocks(result_text)
    # Final fallback: not stream-json at all — return raw output
    logger.warning("stream_json_parse_fallback", output_len=len(stream_output))
    return stream_output


# ── Exceptions ────────────────────────────────────────


# Claude Code built-in tools (no MCP server required)
_BUILTIN_TOOLS = frozenset({
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "WebSearch", "WebFetch", "NotebookEdit", "Task",
    "StructuredOutput",  # Internal tool added by --json-schema
})

# 빈 allowed_tools=[]를 CLI에 전달할 때 쓰는 더미 이름.
# 빈 문자열은 CLI hang을 유발하므로 매칭되지 않는 가짜 이름을 1개 넘겨
# "허용된 툴이 0개"인 상태를 만든다. 이름이 변하면 안 됨 — 빌트인과 충돌 금지.
_NO_TOOLS_SENTINEL = "__none_no_tools_allowed__"


def _all_builtin(tools: list[str] | None) -> bool:
    """Return True if all tools are Claude Code built-ins (no MCP needed)."""
    if tools is None:
        return False
    return len(tools) > 0 and all(t in _BUILTIN_TOOLS for t in tools)


# ── Process tree management ──────────────────────────────

# Global registry: pid → (pgid, session_id)
# session_id enables per-mode cleanup (e.g. "company", "disc", "secretary")
_active_processes: dict[int, tuple[int, str]] = {}
_registry_lock = threading.Lock()

# Thread-local storage for session tagging — set by each mode's runner
_session_local = threading.local()


def set_session_tag(session_id: str) -> None:
    """Tag the current thread/task so spawned subprocesses inherit this session ID."""
    _session_local.session_id = session_id


def get_session_tag() -> str:
    """Get the current thread's session tag (empty string if unset)."""
    return getattr(_session_local, "session_id", "")


def get_pids_by_session(session_id: str) -> set[int]:
    """Return all active PIDs belonging to a specific session."""
    with _registry_lock:
        return {pid for pid, (_, sid) in _active_processes.items() if sid == session_id}


def _register_process(proc: asyncio.subprocess.Process) -> None:
    """Register a subprocess and its process group for cleanup tracking.

    Safety: Only register if the process has its own process group
    (created by start_new_session=True / setsid). If the child shares
    our process group, killpg would kill the entire terminal.
    """
    if proc.pid is None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        # Process already exited — don't register
        logger.debug("register_process_already_exited", pid=proc.pid)
        return

    # Safety check: never register a process whose pgid matches our own.
    # If setsid() failed, the child inherits the parent pgid, and killpg
    # would kill our entire terminal process group.
    my_pgid = os.getpgid(os.getpid())
    if pgid == my_pgid:
        logger.warning(
            "register_process_shared_pgid_skipped",
            pid=proc.pid,
            pgid=pgid,
            reason="child shares parent process group — setsid may have failed",
        )
        # Register with pgid=0 sentinel — _kill_process_tree will use
        # proc.kill() instead of os.killpg() for this process.
        with _registry_lock:
            _active_processes[proc.pid] = (0, get_session_tag())
        return

    with _registry_lock:
        _active_processes[proc.pid] = (pgid, get_session_tag())


def _unregister_process(proc: asyncio.subprocess.Process) -> None:
    """Remove a subprocess from the global registry."""
    if proc.pid is not None:
        with _registry_lock:
            _active_processes.pop(proc.pid, None)


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and all its children via process group.

    Phases: SIGTERM → 5s wait → SIGKILL → 3s reap.
    This ensures MCP server child processes don't survive.

    Safety: If the process shares our process group (pgid sentinel = 0),
    falls back to proc.kill() to avoid killing the entire terminal.
    """
    if proc.pid is None:
        return

    with _registry_lock:
        entry = _active_processes.get(proc.pid)
        pgid = entry[0] if entry else 0

    # Safety: pgid=0 sentinel means shared process group — use proc.kill() only
    use_pgid = pgid > 0

    if use_pgid:
        # Extra safety: verify pgid still belongs to a child, not our terminal
        try:
            my_pgid = os.getpgid(os.getpid())
            if pgid == my_pgid:
                logger.warning(
                    "kill_process_tree_pgid_matches_parent",
                    pid=proc.pid,
                    pgid=pgid,
                )
                use_pgid = False
        except OSError:
            pass

    # Phase 1: SIGTERM — give MCP servers a chance to exit gracefully
    if use_pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    else:
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            pass

    # Phase 2: Wait for graceful shutdown
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        return  # Clean exit
    except (asyncio.TimeoutError, OSError):
        pass

    # Phase 3: SIGKILL — force kill
    if use_pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    else:
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

    # Phase 4: Reap zombie
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except (asyncio.TimeoutError, OSError):
        logger.debug("zombie_reap_failed", pid=proc.pid)


def cleanup_all_subprocesses(grace_period: float = 3.0) -> None:
    """Kill all tracked subprocesses and their process trees.

    Synchronous — safe to call from signal handlers and atexit.
    SIGTERM → grace_period wait → SIGKILL.

    Safety: Skips os.killpg for processes with pgid=0 sentinel
    (shared parent process group) and uses os.kill(pid) instead.
    """
    import time

    with _registry_lock:
        snapshot = dict(_active_processes)
        _active_processes.clear()

    if not snapshot:
        return

    logger.info("cleanup_subprocesses", count=len(snapshot))

    # Get our own pgid to prevent accidental self-kill
    try:
        my_pgid = os.getpgid(os.getpid())
    except OSError:
        my_pgid = 0

    # Phase 1: SIGTERM all process groups
    for pid, (pgid, _sid) in snapshot.items():
        if pgid > 0 and pgid != my_pgid:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        else:
            # Shared pgid or sentinel — kill only the process itself
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    # Phase 2: Wait for graceful shutdown
    time.sleep(grace_period)

    # Phase 3: SIGKILL any survivors
    for pid, (pgid, _sid) in snapshot.items():
        if pgid > 0 and pgid != my_pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


def cleanup_specific_pids(pids: set[int]) -> None:
    """Kill only the specified subprocess PIDs (safe for per-session cleanup).

    Removes them from the global registry and sends SIGTERM → SIGKILL.
    Does NOT affect other sessions' subprocesses.
    """
    if not pids:
        return

    my_pgid = 0
    try:
        my_pgid = os.getpgid(os.getpid())
    except OSError:
        pass

    targets: dict[int, int] = {}
    with _registry_lock:
        for pid in pids:
            if pid in _active_processes:
                pgid, _sid = _active_processes.pop(pid)
                targets[pid] = pgid

    if not targets:
        return

    logger.info("cleanup_specific_pids", count=len(targets), pids=list(targets.keys()))

    # SIGTERM
    for pid, pgid in targets.items():
        if pgid > 0 and pgid != my_pgid:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass

    # Brief grace period then SIGKILL
    import time
    time.sleep(1.0)

    for pid, pgid in targets.items():
        if pgid > 0 and pgid != my_pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


def _emergency_cleanup() -> None:
    """atexit handler: ensure no orphan processes survive Python exit."""
    cleanup_all_subprocesses(grace_period=1.0)


atexit.register(_emergency_cleanup)


# ── Exceptions ────────────────────────────────────────


class ClaudeCodeError(Exception):
    """Claude Code returned an error or non-zero exit."""


class ClaudeCodeTimeoutError(ClaudeCodeError):
    """Claude Code did not respond within the timeout."""

    def __init__(self, message: str):
        super().__init__(message)
        self.partial_stdout: str | None = None
        self.partial_result: BaseModel | None = None


# ── Transport detection ───────────────────────────────

_USE_SDK: bool | None = None


def _sdk_available() -> bool:
    """Lazily check whether ``claude-code-sdk`` is importable."""
    global _USE_SDK
    if _USE_SDK is None:
        try:
            import claude_code_sdk  # noqa: F401

            _USE_SDK = True
        except ImportError:
            _USE_SDK = False
    return _USE_SDK


# ── Bridge ────────────────────────────────────────────


class ClaudeCodeBridge:
    """Thin wrapper around Claude Code headless execution.

    Transport strategy (auto-selected):
      1. Try ``claude-code-sdk`` → async-native SDK mode.
      2. Fall back to ``claude -p …`` subprocess mode.
    """

    def __init__(self, max_retries: int = 1) -> None:
        self.max_retries = max_retries
        self._circuit = _CircuitBreaker()

    async def close(self) -> None:
        """No-op — CLI bridge has no persistent connection to close."""

    # ── Public API ────────────────────────────────────

    async def raw_query(
        self,
        system_prompt: str,
        user_message: str,
        *,
        model: str = "sonnet",
        allowed_tools: list[str] | None = None,
        max_turns: int = 30,
        timeout: int = 900,
        effort: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        extra_dirs: list[str] | None = None,
        on_event: "Callable[[dict], Any] | None" = None,
    ) -> str:
        """Run Claude Code and return the full text from all assistant turns.

        Uses ``--output-format stream-json`` to capture every assistant
        message, not just the last one.  This prevents front-truncation
        when Claude splits a long HTML report across multiple turns.

        Args:
            session_id: Optional UUID to start a new persistent session.
                Subsequent calls can use ``resume`` with the same UUID.
                Cannot be combined with ``resume``.
            resume: Optional session ID to resume an existing conversation.
                The CLI will load the prior conversation state from disk,
                so the user_message should contain ONLY the new turn.
            extra_dirs: Additional directories to grant tool access to via
                ``--add-dir``. Needed when writing to paths outside cwd
                (e.g. ``~/.claude/skills/`` from a ``/tmp`` cwd).
        """
        if not user_message or not user_message.strip():
            user_message = "(context provided in system prompt)"

        if session_id and resume:
            raise ValueError("Cannot specify both session_id and resume")

        cmd = [
            "claude", "-p", user_message,
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
            "--max-turns", str(max_turns),
            "--append-system-prompt", system_prompt,
        ]
        if session_id:
            cmd.extend(["--session-id", session_id])
        elif resume:
            cmd.extend(["--resume", resume])
        if effort:
            cmd.extend(["--effort", effort])
        if allowed_tools is not None:
            # allowed_tools=[] → 모든 툴 차단(센티넬 1개만 화이트리스트).
            # 빈 문자열은 CLI hang 유발하므로 가짜 이름을 1개 넘긴다.
            tools_arg = ",".join(allowed_tools) if allowed_tools else _NO_TOOLS_SENTINEL
            cmd.extend(["--allowedTools", tools_arg])
            # Headless mode (-p) has no TTY for permission prompts.
            # --permission-mode auto pre-approves tools in --allowedTools.
            cmd.extend(["--permission-mode", "auto"])
        if extra_dirs:
            for d in extra_dirs:
                cmd.extend(["--add-dir", d])

        # Skip MCP when all tools are built-ins or none needed
        skip_mcp = (
            (allowed_tools is not None and len(allowed_tools) == 0)
            or _all_builtin(allowed_tools)
        )

        if on_event is not None:
            return await self._run_subprocess_streaming(
                cmd, timeout=timeout, skip_mcp=skip_mcp, on_event=on_event,
            )

        raw = await self._run_subprocess(cmd, timeout=timeout, skip_mcp=skip_mcp)
        return _extract_all_assistant_text(raw)

    async def structured_query(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: type[BaseModel],
        model: str = "sonnet",
        allowed_tools: list[str] | None = None,
        timeout: int = 120,
        max_turns: int | None = None,
        effort: str | None = None,
        time_budget: float | None = None,
        progress_callback=None,  # Accepted for API compatibility; CLI has no turn loop
        max_tokens: int | None = None,  # API 호환성; CLI에서는 무시
    ) -> BaseModel:
        """Run a Claude Code query and parse the result into *output_schema*.

        Args:
            system_prompt: System-level instruction appended via ``--append-system-prompt``.
            user_message: The user-facing prompt (``-p``).
            output_schema: Pydantic model class whose JSON schema is enforced.
            model: Claude Code model id (``"opus"``, ``"sonnet"``, ``"haiku"``).
            allowed_tools: Optional list of Claude Code tool names to enable.
            timeout: Seconds before giving up.
            max_turns: Maximum Claude CLI interaction rounds. None = CLI default.

        Returns:
            A validated *output_schema* instance.

        Raises:
            ClaudeCodeError: On execution failure.
            ClaudeCodeTimeoutError: On timeout.
        """
        import random
        from src.config.settings import get_settings
        settings = get_settings()

        # B3: Circuit breaker — fast-fail if too many consecutive errors
        if not self._circuit.can_proceed():
            raise ClaudeCodeError("Circuit breaker open: too many consecutive failures. Retry after cooldown.")

        last_error: Exception = ClaudeCodeError("structured_query: all attempts failed")
        max_attempts = 1 + settings.retry_max_attempts
        query_start = _time_mod.monotonic()

        for attempt in range(max_attempts):
            # A2: Budget check — 남은 시간이 30초 미만이면 retry 중단
            if time_budget is not None and attempt > 0:
                elapsed = _time_mod.monotonic() - query_start
                remaining = time_budget - elapsed
                if remaining < 30:
                    logger.warning(
                        "retry_budget_exhausted",
                        attempt=attempt,
                        remaining=round(remaining, 1),
                    )
                    break  # raise last_error below

            # A2: 이번 attempt의 timeout은 남은 budget과 설정값 중 작은 값
            if time_budget is not None:
                elapsed = _time_mod.monotonic() - query_start
                remaining = time_budget - elapsed
                effective_timeout = max(30, min(timeout, int(remaining)))
            else:
                effective_timeout = timeout

            try:
                use_sdk = _sdk_available() and not settings.prefer_subprocess

                if use_sdk:
                    try:
                        result = await self._sdk_query(
                            system_prompt, user_message, output_schema,
                            model=model, allowed_tools=allowed_tools, timeout=effective_timeout,
                            max_turns=max_turns, effort=effort,
                        )
                        self._circuit.record_success()
                        return result
                    except ClaudeCodeTimeoutError:
                        raise
                    except Exception as sdk_exc:
                        err_str = str(sdk_exc)
                        if "rate_limit" in err_str:
                            logger.debug("sdk_rate_limit_fallback", error=err_str)
                        else:
                            logger.warning("sdk_error_fallback_to_subprocess", error=err_str)
                        result = await self._subprocess_query(
                            system_prompt, user_message, output_schema,
                            model=model, allowed_tools=allowed_tools, timeout=effective_timeout,
                            max_turns=max_turns, effort=effort,
                        )
                        self._circuit.record_success()
                        return result

                result = await self._subprocess_query(
                    system_prompt, user_message, output_schema,
                    model=model, allowed_tools=allowed_tools, timeout=effective_timeout,
                    max_turns=max_turns, effort=effort,
                )
                self._circuit.record_success()
                return result

            except ClaudeCodeTimeoutError as te:
                self._circuit.record_failure()
                # B3: partial result가 있으면 반환 (B4: is_partial 플래그 설정)
                if te.partial_result is not None:
                    logger.warning("timeout_with_partial_result", schema=output_schema.__name__)
                    if hasattr(te.partial_result, "is_partial"):
                        te.partial_result.is_partial = True
                    return te.partial_result
                raise

            except ClaudeCodeError as exc:
                last_error = exc
                self._circuit.record_failure()

                # B2: Error-category-aware retry with exponential backoff
                category = classify_error(str(exc))
                retried = False

                if category == ErrorCategory.AUTH_ERROR:
                    raise  # auth errors are not retryable

                if category == ErrorCategory.MAX_TURNS:
                    raise  # max_turns → tier fallback handles this, retry wastes budget

                if category == ErrorCategory.RATE_LIMIT and attempt < settings.retry_max_attempts:
                    delay = min(
                        settings.retry_base_delay * (2 ** attempt)
                        + random.uniform(0, settings.retry_jitter_max),
                        settings.retry_max_delay,
                    )
                    logger.warning("rate_limit_backoff", attempt=attempt + 1, delay_s=round(delay, 1))
                    _metrics.record(0, success=False, retried=True)
                    await asyncio.sleep(delay)
                    continue

                if category in (ErrorCategory.SERVER_ERROR, ErrorCategory.OVERLOADED):
                    if attempt < settings.retry_server_error_attempts:
                        delay = settings.retry_base_delay + random.uniform(0, settings.retry_jitter_max)
                        logger.warning("server_error_retry", attempt=attempt + 1, category=category.value)
                        _metrics.record(0, success=False, retried=True)
                        await asyncio.sleep(delay)
                        continue

                # CLI_ERROR / UNKNOWN: one retry with jitter
                if attempt == 0:
                    jitter = random.uniform(0.5, settings.retry_jitter_max)
                    logger.warning("claude_code_retry", attempt=1, error=str(exc)[:200], jitter_s=round(jitter, 2))
                    _metrics.record(0, success=False, retried=True)
                    await asyncio.sleep(jitter)
                    continue

                raise

            except Exception as exc:
                last_error = exc
                self._circuit.record_failure()
                if attempt == 0:
                    jitter = random.uniform(0.5, settings.retry_jitter_max)
                    logger.warning("claude_code_retry", attempt=1, error=str(exc)[:200])
                    await asyncio.sleep(jitter)
                    continue
                raise

        raise last_error

    # ── SDK transport ─────────────────────────────────

    @traceable_llm(name="claude_code_sdk")
    async def _sdk_query(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: type[BaseModel],
        *,
        model: str,
        allowed_tools: list[str] | None,
        timeout: int,
        max_turns: int | None = None,
        effort: str | None = None,
    ) -> BaseModel:
        from claude_code_sdk import query as sdk_query, ClaudeCodeOptions

        schema_json = json.dumps(output_schema.model_json_schema(), ensure_ascii=False)

        extra_args: dict[str, str] = {"json-schema": schema_json}
        if max_turns is not None:
            extra_args["max-turns"] = str(max_turns)
        if effort:
            extra_args["effort"] = effort

        options = ClaudeCodeOptions(
            model=model,
            append_system_prompt=system_prompt,
            allowed_tools=allowed_tools or [],
            extra_args=extra_args,
        )

        _active_gen = None  # Track async generator for cleanup on timeout

        async def _inner() -> BaseModel:
            nonlocal _active_gen
            # Temporarily unset CLAUDECODE to avoid "nested session" detection.
            # Use a lock so concurrent SDK calls don't race on os.environ.
            with _env_lock:
                saved = os.environ.pop("CLAUDECODE", None)
            try:
                structured_data = None
                gen = sdk_query(prompt=user_message, options=options)
                _active_gen = gen
                async for event in gen:
                    # AssistantMessage with StructuredOutput tool use
                    if hasattr(event, "content") and isinstance(event.content, list):
                        for block in event.content:
                            if (hasattr(block, "name") and block.name == "StructuredOutput"
                                    and hasattr(block, "input")):
                                structured_data = block.input

                    # ResultMessage — check for errors
                    if hasattr(event, "subtype"):
                        if event.subtype == "error" or (hasattr(event, "is_error") and event.is_error):
                            msg = getattr(event, "result", "") or "SDK error"
                            raise ClaudeCodeError(msg)
                _active_gen = None
            finally:
                with _env_lock:
                    if saved is not None:
                        os.environ["CLAUDECODE"] = saved

            if structured_data is not None:
                return output_schema.model_validate(structured_data)

            raise ClaudeCodeError("SDK query completed without structured output")

        try:
            return await asyncio.wait_for(_inner(), timeout=timeout)
        except asyncio.TimeoutError:
            if _active_gen is not None:
                try:
                    await _active_gen.aclose()
                except Exception:
                    pass
            raise ClaudeCodeTimeoutError(f"SDK query timed out after {timeout}s")
        except asyncio.CancelledError:
            if _active_gen is not None:
                try:
                    await _active_gen.aclose()
                except Exception:
                    pass
            raise

    @staticmethod
    def _parse_sdk_result(raw: str, output_schema: type[BaseModel]) -> BaseModel:
        """Parse SDK result string into output_schema, handling structured_output."""
        try:
            output = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # raw is already the content string — try direct parse
            return output_schema.model_validate_json(raw if isinstance(raw, str) else json.dumps(raw))

        # Check for structured_output (same as subprocess path)
        structured = output.get("structured_output") if isinstance(output, dict) else None
        if structured is None and isinstance(output, dict):
            structured = output.get("result", output)
        if structured is None:
            structured = output

        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except json.JSONDecodeError:
                pass

        return output_schema.model_validate(structured)

    # ── Subprocess transport ──────────────────────────

    @traceable_llm(name="claude_code_subprocess")
    async def _subprocess_query(
        self,
        system_prompt: str,
        user_message: str,
        output_schema: type[BaseModel],
        *,
        model: str,
        allowed_tools: list[str] | None,
        timeout: int,
        max_turns: int | None = None,
        effort: str | None = None,
    ) -> BaseModel:
        """Run ``claude -p`` and parse JSON output into *output_schema*."""
        schema_json = json.dumps(output_schema.model_json_schema(), ensure_ascii=False)

        # Claude CLI requires non-empty prompt with --print flag
        if not user_message or not user_message.strip():
            user_message = "(context provided in system prompt)"

        cmd = [
            "claude", "-p", user_message,
            "--output-format", "json",
            "--json-schema", schema_json,
            "--model", model,
            "--append-system-prompt", system_prompt,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if max_turns is not None:
            cmd.extend(["--max-turns", str(max_turns)])
        if allowed_tools is not None:
            # allowed_tools=["Read", ...] → "--allowedTools Read,Write,..."
            # allowed_tools=[] → 센티넬 1개만 화이트리스트해서 모든 툴 차단
            #   (빈 문자열은 CLI hang 유발이라 사용 불가).
            # allowed_tools=None → flag omitted (Claude Code default, all tools).
            tools_arg = ",".join(allowed_tools) if allowed_tools else _NO_TOOLS_SENTINEL
            cmd.extend(["--allowedTools", tools_arg])
            # Headless mode (-p) has no TTY for permission prompts.
            # --permission-mode auto pre-approves tools listed in --allowedTools
            # so workers don't get "권한 미허용" errors for WebSearch/WebFetch etc.
            cmd.extend(["--permission-mode", "auto"])

        # Skip MCP server startup when possible:
        # 1. allowed_tools=[] → no tools needed (CEO/Leader)
        # 2. all tools are Claude Code built-ins → MCP servers not needed
        # Both cases: run from /tmp to avoid .mcp.json discovery (saves 30-60s)
        skip_mcp = (
            (allowed_tools is not None and len(allowed_tools) == 0)
            or _all_builtin(allowed_tools)
        )
        try:
            raw = await self._run_subprocess(cmd, timeout=timeout, skip_mcp=skip_mcp)
        except ClaudeCodeTimeoutError as te:
            # B2: timeout이지만 partial stdout이 있으면 recovery 시도
            partial = te.partial_stdout
            if partial:
                recovered = _try_partial_recovery(partial, output_schema)
                if recovered is not None:
                    logger.warning(
                        "timeout_partial_recovery_success",
                        schema=output_schema.__name__,
                        partial_size=len(partial),
                    )
                    te.partial_result = recovered
                    _metrics.record_partial_recovery()
            raise

        try:
            output = json.loads(_sanitize_json_output(raw))
        except json.JSONDecodeError as e:
            # B4: attempt partial recovery before giving up
            recovered = _try_partial_recovery(raw, output_schema)
            if recovered is not None:
                logger.warning("json_partial_recovery", pos=e.pos, schema=output_schema.__name__)
                return recovered
            raise ClaudeCodeError(
                f"Claude Code returned non-JSON output (pos {e.pos})"
            ) from e

        if output.get("is_error"):
            raise ClaudeCodeError(output.get("result", "Unknown error"))

        # Detect max_turns exhaustion — CLI returns subtype "error_max_turns"
        # with is_error=False.  Try salvaging structured_output before raising.
        if output.get("subtype") == "error_max_turns":
            _partial = output.get("structured_output") or output.get("result")
            if _partial:
                if isinstance(_partial, str):
                    try:
                        _partial = json.loads(_partial)
                    except json.JSONDecodeError:
                        _partial = None
                if _partial and isinstance(_partial, dict):
                    try:
                        result = output_schema.model_validate(_partial)
                        logger.warning(
                            "max_turns_partial_recovery",
                            schema=output_schema.__name__,
                            num_turns=output.get("num_turns"),
                        )
                        if hasattr(result, "is_partial"):
                            result.is_partial = True
                        _metrics.record_partial_recovery()
                        return result
                    except Exception:
                        pass

            # No salvageable structured output — raise with partial context
            err = ClaudeCodeError(
                f"Claude Code hit max_turns limit (num_turns={output.get('num_turns')})"
            )
            # Attach result text so Tier 2 can resume from Tier 1 progress
            _result_text = output.get("result", "")
            if isinstance(_result_text, str) and len(_result_text) > 50:
                err.partial_context = _result_text
            raise err

        # Claude Code returns structured output in "structured_output" when
        # --json-schema is used, or in "result" for plain --output-format json.
        structured = output.get("structured_output")
        if structured is None:
            structured = output.get("result", output)
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except json.JSONDecodeError as e:
                raise ClaudeCodeError(
                    f"Claude Code result field is not valid JSON (pos {e.pos})"
                ) from e

        # Strip ★ Insight plugin contamination from all string values
        structured = _clean_insight_from_dict(structured)

        try:
            return output_schema.model_validate(structured)
        except Exception as e:
            raise ClaudeCodeError(
                f"Schema validation failed for {output_schema.__name__}: {e}"
            ) from e

    @staticmethod
    async def _run_subprocess_streaming(
        cmd: list[str], *, timeout: int, skip_mcp: bool = False,
        on_event: "Callable[[dict], Any]",
    ) -> str:
        """Execute with line-by-line NDJSON streaming and on_event callbacks."""
        import time as _t

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        cwd: str | None = "/tmp" if skip_mcp else (
            os.environ.get("ENTERPRISE_AGENT_ROOT") or None
        )

        _no_plugin_dir = "/tmp/claude-no-plugins"
        os.makedirs(_no_plugin_dir, exist_ok=True)
        cmd = list(cmd)
        cmd.extend(["--plugin-dir", _no_plugin_dir])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
            limit=sys.maxsize,
        )
        _register_process(proc)

        texts: list[str] = []
        result_text = ""
        t0 = _t.monotonic()

        try:
            async with asyncio.timeout(timeout):
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")
                    elapsed = round(_t.monotonic() - t0, 1)

                    if event_type == "assistant":
                        message = event.get("message", {})
                        for block in message.get("content", []):
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                texts.append(block["text"])
                            elif block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                tool_input = block.get("input", {})
                                cb_result = on_event({
                                    "action": "tool_use",
                                    "tool": tool_name,
                                    "input": tool_input,
                                    "elapsed": elapsed,
                                })
                                if asyncio.iscoroutine(cb_result):
                                    await cb_result

                    elif event_type == "result":
                        if event.get("is_error"):
                            err_text = event.get("result", "Unknown error")
                            if not texts:
                                raise ClaudeCodeError(err_text)
                        result_text = event.get("result", "")

        except TimeoutError:
            logger.warning("streaming_subprocess_timeout", timeout=timeout)
            try:
                proc.terminate()
            except Exception:
                pass
            # terminate 후 1초 내 종료 안 되면 kill로 강제 종료.
            # 이걸 안 하면 proc.wait()가 영원히 block되어 타임아웃이 무의미해짐.
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass

        await proc.wait()
        _unregister_process(proc)

        if texts:
            return _strip_insight_blocks("\n".join(texts))
        if result_text:
            return _strip_insight_blocks(result_text)
        return ""

    @staticmethod
    async def _run_subprocess(
        cmd: list[str], *, timeout: int, skip_mcp: bool = False,
    ) -> str:
        """Execute a command and return stdout.

        Args:
            skip_mcp: If True, run from /tmp so Claude Code doesn't discover
                      .mcp.json and start MCP servers (saves 30-60s per call).
        """
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        if skip_mcp:
            cwd = "/tmp"
            # /tmp has no .mcp.json → MCP servers won't start.
            # NOTE: Do NOT set CLAUDE_CODE_SIMPLE here — it blocks OAuth/keychain
            # auth and forces ANTHROPIC_API_KEY only, causing "Not logged in" errors.
        else:
            project_root = os.environ.get("ENTERPRISE_AGENT_ROOT", "")
            cwd = project_root or None

        # Prevent output-style plugins (learning, explanatory) from contaminating
        # worker output with ★ Insight blocks. Load plugins from empty directory.
        _no_plugin_dir = "/tmp/claude-no-plugins"
        os.makedirs(_no_plugin_dir, exist_ok=True)
        cmd = list(cmd)  # defensive copy
        cmd.extend(["--plugin-dir", _no_plugin_dir])

        # Extract model and MCP info for diagnostic logging
        _model = ""
        for i, arg in enumerate(cmd):
            if arg == "--model" and i + 1 < len(cmd):
                _model = cmd[i + 1]
                break
        _has_mcp = not skip_mcp
        logger.debug(
            "subprocess_start",
            model=_model,
            mcp=_has_mcp,
            timeout=timeout,
            cwd=cwd or "(inherited)",
        )

        _t0 = _time_mod.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
            limit=sys.maxsize,
        )
        _register_process(proc)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            _elapsed = _time_mod.monotonic() - _t0
            _metrics.record(_elapsed, success=False, timeout=True)
            logger.warning(
                "subprocess_timeout",
                error_type=ErrorCategory.TIMEOUT.value,
                model=_model,
                mcp=_has_mcp,
                elapsed_s=round(_elapsed, 1),
                timeout=timeout,
            )

            # B1: Partial stdout salvage — SIGTERM + read remaining pipe data
            partial_stdout = None
            try:
                proc.terminate()  # SIGTERM (graceful)
                try:
                    # proc.communicate() can't be called again after wait_for cancels it.
                    # Read directly from the stdout pipe instead.
                    stdout_buf = await asyncio.wait_for(
                        proc.stdout.read(), timeout=3.0,
                    ) if proc.stdout else b""
                    if stdout_buf:
                        partial_stdout = stdout_buf.decode(errors="replace").strip()
                        logger.info(
                            "partial_stdout_salvaged",
                            size=len(partial_stdout),
                            model=_model,
                        )
                except asyncio.TimeoutError:
                    pass  # 3s 내 응답 없으면 포기
            except ProcessLookupError:
                pass  # 이미 종료됨

            await _kill_process_tree(proc)

            err = ClaudeCodeTimeoutError(
                f"Claude Code subprocess timed out after {timeout}s (model={_model}, mcp={_has_mcp})"
            )
            err.partial_stdout = partial_stdout
            raise err
        except BaseException:
            await _kill_process_tree(proc)
            raise
        finally:
            _unregister_process(proc)
            _elapsed = _time_mod.monotonic() - _t0
            logger.debug(
                "subprocess_done",
                model=_model,
                mcp=_has_mcp,
                elapsed_s=round(_elapsed, 1),
                returncode=proc.returncode,
            )

        # A3: stdout size limit
        from src.config.settings import get_settings
        if stdout and len(stdout) > get_settings().max_output_size:
            logger.warning(
                "subprocess_output_truncated",
                original_size=len(stdout),
                max_size=get_settings().max_output_size,
            )
            stdout = stdout[:get_settings().max_output_size]

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip() if stderr else ""
            stdout_str = stdout.decode(errors="replace").strip() if stdout else ""

            # CLI stream-json returns NDJSON - parse each line to find is_error result
            if stdout_str:
                result_err = None
                for line in stdout_str.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "result" and obj.get("is_error"):
                            result_err = (
                                f"subtype={obj.get('subtype','?')} "
                                f"reason={obj.get('terminal_reason','?')} "
                                f"turns={obj.get('num_turns','?')} "
                                f"result={str(obj.get('result',''))[:200]}"
                            )
                            break
                    except (json.JSONDecodeError, ValueError):
                        continue
                if result_err:
                    err_msg = result_err
                elif not err_msg:
                    # No result line and no stderr - try single-JSON fallback
                    try:
                        out_json = json.loads(_sanitize_json_output(stdout_str))
                        if out_json.get("is_error"):
                            err_msg = out_json.get("result", "unknown error")
                    except (json.JSONDecodeError, ValueError):
                        err_msg = stdout_str[:300]
            if not err_msg:
                err_msg = "unknown error"

            category = classify_error(err_msg, proc.returncode)
            _metrics.record(_elapsed, success=False)
            logger.error(
                "subprocess_failed",
                error_type=category.value,
                exit_code=proc.returncode,
                stderr_snippet=err_msg[:300],
                stdout_snippet=stdout_str[:300] if stdout_str else "",
                elapsed_s=round(_elapsed, 1),
                model=_model,
                mcp=_has_mcp,
            )
            raise ClaudeCodeError(
                f"[{category.value}] Claude Code exited {proc.returncode}: {err_msg[:200]}"
            )

        _metrics.record(_elapsed, success=True)
        return stdout.decode(errors="replace").strip()
