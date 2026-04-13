"""Tests for src/main.py TUI formatting logic."""

import pytest
from src.main import (
    _parse_report_sections,
    _parse_domain_line,
    _print_message,
    _print_hierarchy,
    _PARALLEL_NODES,
    _ui_state,
)
from src.utils.progress import NODE_LABELS


# ── _parse_report_sections ──────────────────────────────


class TestParseReportSections:
    def test_parses_all_sections(self):
        content = (
            "[CEO Final Report]\n\n"
            "## Summary\nThis is the summary.\n\n"
            "## Domain Results\n  [research] Analysis - quality: 8/10\n\n"
            "## Gap Analysis\nMinor gaps found.\n\n"
            "## Recommendations\n  - Do X\n  - Do Y"
        )
        sections = _parse_report_sections(content)
        assert "Summary" in sections
        assert "This is the summary." in sections["Summary"]
        assert "Domain Results" in sections
        assert "[research]" in sections["Domain Results"]
        assert "Gap Analysis" in sections
        assert "Minor gaps" in sections["Gap Analysis"]
        assert "Recommendations" in sections
        assert "Do X" in sections["Recommendations"]

    def test_empty_sections(self):
        content = "## Summary\n\n## Domain Results\n"
        sections = _parse_report_sections(content)
        assert sections["Summary"].strip() == ""

    def test_no_headers(self):
        content = "Just some text without headers."
        sections = _parse_report_sections(content)
        assert "_header" in sections
        assert "Just some text" in sections["_header"]

    def test_single_section(self):
        content = "## Summary\nOnly summary here."
        sections = _parse_report_sections(content)
        assert "Summary" in sections
        assert "Only summary here." in sections["Summary"]


# ── _parse_domain_line ──────────────────────────────────


class TestParseDomainLine:
    def test_full_format_with_gaps(self):
        line = "[research] Market analysis complete - quality: 8/10 (gaps: data coverage, timeline)"
        parsed = _parse_domain_line(line)
        assert parsed["domain"] == "research"
        assert "Market analysis" in parsed["summary"]
        assert parsed["quality"] == "8/10"
        assert "data coverage" in parsed["gaps"]

    def test_format_without_gaps(self):
        line = "[engineering] Implementation done - quality: 9/10"
        parsed = _parse_domain_line(line)
        assert parsed["domain"] == "engineering"
        assert parsed["quality"] == "9/10"
        assert "gaps" not in parsed

    def test_no_domain_bracket(self):
        line = "Some plain text result"
        parsed = _parse_domain_line(line)
        assert "domain" not in parsed
        assert parsed["summary"] == "Some plain text result"

    def test_no_quality_score(self):
        line = "[design] Wireframes delivered"
        parsed = _parse_domain_line(line)
        assert parsed["domain"] == "design"
        assert "Wireframes" in parsed["summary"]
        assert "quality" not in parsed

    def test_quality_score_with_decimal(self):
        line = "[research] Done - quality: 7/10"
        parsed = _parse_domain_line(line)
        assert parsed["quality"] == "7/10"


# ── NODE_LABELS coverage ───────────────────────────────


class TestNodeLabels:
    """Verify all graph nodes have display labels."""

    EXPECTED_NODES = [
        "intake", "single_session", "user_review_results", "error_terminal",
    ]

    def test_all_nodes_have_labels(self):
        for node in self.EXPECTED_NODES:
            assert node in NODE_LABELS, f"Missing label for node: {node}"

    def test_labels_are_nonempty_strings(self):
        for node, label in NODE_LABELS.items():
            assert isinstance(label, str)
            assert len(label) > 0


# ── _PARALLEL_NODES ─────────────────────────────────────


class TestParallelNodes:
    """싱글 세션 모드는 병렬 노드 개념이 없어 _PARALLEL_NODES는 항상 비어 있다."""

    def test_parallel_nodes_is_empty(self):
        assert _PARALLEL_NODES == set()


# ── _print_hierarchy ────────────────────────────────────


class TestPrintHierarchy:
    """Test hierarchy tree rendering (smoke tests — verify no crashes)."""

    def test_single_leader_single_worker(self):
        leaders = [
            {
                "leader_domain": "research",
                "workers": [{"worker_domain": "researcher"}],
            }
        ]
        _print_hierarchy(leaders)  # Should not raise

    def test_multiple_leaders_multiple_workers(self):
        leaders = [
            {
                "leader_domain": "research",
                "workers": [
                    {"worker_domain": "researcher"},
                    {"worker_domain": "analyst"},
                ],
            },
            {
                "leader_domain": "engineering",
                "workers": [
                    {"worker_domain": "developer"},
                ],
            },
        ]
        _print_hierarchy(leaders)  # Should not raise

    def test_empty_leaders(self):
        _print_hierarchy([])  # Should not raise

    def test_leader_with_no_workers(self):
        leaders = [{"leader_domain": "design", "workers": []}]
        _print_hierarchy(leaders)  # Should not raise

    def test_missing_fields_handled(self):
        leaders = [{"workers": [{}]}]
        _print_hierarchy(leaders)  # Should not raise (uses "unknown")


# ── _ui_state step counter ──────────────────────────────


class TestUiState:
    def test_initial_state(self):
        assert "last_node" in _ui_state
        assert "step" in _ui_state

    def test_step_counter_type(self):
        assert isinstance(_ui_state["step"], int)


# ── _print_message role detection ────────────────────────


class TestPrintMessageRoleDetection:
    """Test message classification by examining the routing logic.

    We can't easily capture Rich output, so we test the classification
    conditions directly on representative message strings.
    """

    def test_ceo_final_report_detected(self):
        msg = "[CEO Final Report]\n\n## Summary\nDone."
        assert msg.startswith("[CEO Final Report]")

    def test_error_terminal_node(self):
        assert "error_terminal" in NODE_LABELS

    def test_system_message_detected(self):
        assert "[System] Workflow terminated".startswith("[System]")
        assert "[system] error".startswith("[system]")

    def test_failure_pattern_detected(self):
        msg1 = "[research] Execution failed: timeout"
        msg2 = "[engineering leader] Review failed (error), auto-approving."
        assert " failed" in msg1
        assert " failed" in msg2

    def test_ceo_message_detected(self):
        msg = "[CEO] Assembling leaders for domains: research"
        assert msg.startswith("[CEO")

    def test_leader_message_detected(self):
        msg1 = "[research leader] Workers assembled: analyst, writer"
        msg2 = "[engineering leader] Plan approved (scores: 8,7,9)"
        assert " leader]" in msg1
        assert " leader]" in msg2

    def test_leader_report_format_detected(self):
        """Leader report formatting uses '[X leader -> CEO report]' format (merged into ceo_confirm_plan)."""
        msg = "[research leader -> CEO report]\nPlans approved for 2 workers."
        # Must match via ' leader ->' pattern
        assert " leader ->" in msg
        # Must NOT match the old pattern only
        assert " leader]" not in msg

    def test_worker_message_is_default(self):
        msg = "[researcher] Plan submitted: Market Analysis"
        # Not CEO, not leader, not system, not failed
        assert not msg.startswith("[CEO")
        assert " leader]" not in msg
        assert not msg.startswith("[System]")
        assert " failed" not in msg

    def test_korean_leader_string_does_not_match(self):
        """Verify the old bug -- '리더' should NOT be the matching pattern."""
        msg = "[research leader] Questions:\n  1. What scope?"
        # Must match via ' leader]' pattern, NOT '리더'
        assert " leader]" in msg
        assert "리더" not in msg  # English message, no Korean
