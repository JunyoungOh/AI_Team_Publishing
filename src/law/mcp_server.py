"""Law MCP server — exposes law.go.kr tools over stdio JSON-RPC.

Spawned by Claude Code CLI via ``.mcp.json`` when a law session starts.
Reuses the existing ``src/law/tools.py`` executors without modification —
this file is only the MCP protocol adapter.

Why this exists
---------------
The old ``src/law/engine.py`` wrapped tool calls in ``<tool_call>`` XML
blocks that the engine parsed manually. Claude (Sonnet) trained on ReAct
traces tends to fabricate ``<tool_response>`` blocks right after any
``<tool_call>`` it emits, leading to hallucinated articles, MST values,
and precedent numbers that look correct but don't exist. The fabrication
is less visible in the law tab than in DART because law queries usually
resolve in 2 turns, but the structural risk is identical.

Switching to MCP (native Anthropic tool-use protocol) eliminates the
hallucination window entirely — Claude emits a ``tool_use`` block via the
SDK's official channel, CLI intercepts and forwards to this server, and
the real result comes back as ``tool_result`` in the next turn. There is
no text position where Claude can fabricate a response.

Launch (via Claude Code CLI + .mcp.json):
    python3 -m src.law.mcp_server
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from src.law.tools import (
    LAW_TOOL_EXECUTORS,
    LAW_TOOL_SCHEMAS,
    make_session_context,
)

# All logging goes to stderr — stdout is reserved for the MCP stdio protocol.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s law-mcp: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("law-mcp")


# One server, one shared tool context per process. The law.go.kr TTL cache
# lives inside the context so repeated queries within a session benefit
# from warm caches.
server: Server = Server("law-mcp", version="1.0.0")
_ctx: dict[str, Any] = make_session_context()


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Expose all six law tools to Claude via MCP."""
    tools: list[types.Tool] = []
    for name, schema in LAW_TOOL_SCHEMAS.items():
        tools.append(
            types.Tool(
                name=name,
                description=schema.get("description", ""),
                inputSchema=schema.get("input_schema") or {"type": "object"},
            )
        )
    return tools


@server.call_tool()
async def call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[types.TextContent]:
    """Execute one law tool and return its JSON result as text content.

    Errors (including LawAPIError, TypeError for bad args, unexpected
    exceptions) are converted to plain-text error messages so Claude can
    read them and recover on the next turn.
    """
    logger.info("call_tool name=%s args=%s", name, arguments)
    executor = LAW_TOOL_EXECUTORS.get(name)
    if executor is None:
        return [types.TextContent(type="text", text=f"Error: unknown tool '{name}'")]
    try:
        result = await executor(_ctx, **(arguments or {}))
    except TypeError as exc:
        logger.warning("Bad arguments for %s: %s", name, exc)
        return [
            types.TextContent(type="text", text=f"Error: invalid input for {name} — {exc}")
        ]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tool %s failed", name)
        return [
            types.TextContent(
                type="text",
                text=f"Error executing {name}: {type(exc).__name__}: {exc}",
            )
        ]
    return [types.TextContent(type="text", text=result)]


async def main() -> None:
    logger.info("Law MCP server starting (tools=%d)", len(LAW_TOOL_SCHEMAS))
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
