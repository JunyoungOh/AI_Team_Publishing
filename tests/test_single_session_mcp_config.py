"""Unit tests for single_session runtime MCP config injection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.graphs.nodes.single_session import (
    _SESSION_TOOLS,
    _build_runtime_mcp_config,
    _filter_session_tools,
)


# ── _filter_session_tools ───────────────────────────────

def test_filter_keeps_non_mcp_tools_when_no_servers_enabled():
    tools = ["WebSearch", "Read", "mcp__firecrawl__firecrawl_scrape"]
    result = _filter_session_tools(tools, frozenset())
    assert result == ["WebSearch", "Read"]


def test_filter_keeps_mcp_tool_when_server_enabled():
    tools = ["WebFetch", "mcp__firecrawl__firecrawl_scrape"]
    result = _filter_session_tools(tools, frozenset({"firecrawl"}))
    assert result == tools


def test_filter_drops_mcp_tool_for_disabled_server():
    tools = [
        "Read",
        "mcp__firecrawl__firecrawl_scrape",
        "mcp__github__search_code",
    ]
    result = _filter_session_tools(tools, frozenset({"firecrawl"}))
    assert result == ["Read", "mcp__firecrawl__firecrawl_scrape"]


def test_filter_handles_hyphenated_server_name():
    tools = ["mcp__brave-search__brave_web_search"]
    result = _filter_session_tools(tools, frozenset({"brave-search"}))
    assert result == tools


def test_session_tools_constant_still_includes_firecrawl():
    # Sanity: regression guard for the default tool list
    assert "mcp__firecrawl__firecrawl_scrape" in _SESSION_TOOLS
    assert "WebSearch" in _SESSION_TOOLS


# ── _build_runtime_mcp_config ───────────────────────────

@pytest.fixture
def fake_mcp_template(tmp_path, monkeypatch):
    """Create a fake .mcp.json in a tmp directory and chdir there."""
    template = tmp_path / ".mcp.json"
    template.write_text(json.dumps({
        "mcpServers": {
            "firecrawl": {
                "command": "npx",
                "args": ["-y", "firecrawl-mcp"],
                "env": {"FIRECRAWL_API_KEY": "${FIRECRAWL_API_KEY}"},
            },
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
            },
            "static": {
                "command": "echo",
                "args": ["hi"],
            },
        },
    }))
    monkeypatch.chdir(tmp_path)
    return template


def _read_written_config(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_build_returns_none_when_template_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, enabled = _build_runtime_mcp_config()
    assert path is None
    assert enabled == frozenset()


def test_build_substitutes_env_vars(fake_mcp_template, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-abc123")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp-xyz")

    path, enabled = _build_runtime_mcp_config()
    assert path is not None
    try:
        config = _read_written_config(path)
        fc_env = config["mcpServers"]["firecrawl"]["env"]
        gh_env = config["mcpServers"]["github"]["env"]
        assert fc_env["FIRECRAWL_API_KEY"] == "fc-abc123"
        assert gh_env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp-xyz"
        assert "firecrawl" in enabled
        assert "github" in enabled
        assert "static" in enabled  # no env block → preserved
    finally:
        os.unlink(path)


def test_build_prunes_server_with_empty_env(fake_mcp_template, monkeypatch):
    # Only firecrawl key set; github should be pruned.
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-only")
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)

    path, enabled = _build_runtime_mcp_config()
    assert path is not None
    try:
        config = _read_written_config(path)
        assert "firecrawl" in config["mcpServers"]
        assert "github" not in config["mcpServers"]
        assert enabled == frozenset({"firecrawl", "static"})
    finally:
        os.unlink(path)


def test_build_returns_none_when_all_servers_pruned(tmp_path, monkeypatch):
    template = tmp_path / ".mcp.json"
    template.write_text(json.dumps({
        "mcpServers": {
            "firecrawl": {
                "command": "npx",
                "args": ["-y", "firecrawl-mcp"],
                "env": {"FIRECRAWL_API_KEY": "${FIRECRAWL_API_KEY}"},
            },
        },
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    path, enabled = _build_runtime_mcp_config()
    assert path is None
    assert enabled == frozenset()


def test_build_handles_malformed_json(tmp_path, monkeypatch):
    (tmp_path / ".mcp.json").write_text("{ not valid json")
    monkeypatch.chdir(tmp_path)

    path, enabled = _build_runtime_mcp_config()
    assert path is None
    assert enabled == frozenset()
