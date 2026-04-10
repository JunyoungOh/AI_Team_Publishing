"""개발 모드 runner 유닛 테스트."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.overtime.dev_prompts import build_dev_system_prompt
from src.overtime.dev_runner import _get_rate_limit_wait, _COMPLETION_MARKER, MAX_SESSIONS


def test_dev_system_prompt_contains_all_phases():
    prompt = build_dev_system_prompt("test app", "answers", "/tmp/t")
    for phase in ["Phase 1", "Phase 2", "Phase 3", "Phase 4", "Phase 5", "Phase 6"]:
        assert phase in prompt


def test_dev_system_prompt_enforces_local():
    prompt = build_dev_system_prompt("test", "a", "/tmp/t")
    assert "로컬" in prompt


def test_dev_system_prompt_no_mcp_tools():
    prompt = build_dev_system_prompt("test", "a", "/tmp/t")
    assert "mcp__" not in prompt


def test_completion_marker_in_prompt():
    prompt = build_dev_system_prompt("test", "a", "/tmp/t")
    assert _COMPLETION_MARKER in prompt


def test_handoff_context_from_end():
    """handoff 시 PROGRESS.md의 뒤쪽 3000자가 사용되는지 확인."""
    prompt = build_dev_system_prompt(
        "test", "a", "/tmp/t",
        handoff_context="recent progress info",
    )
    assert "recent progress info" in prompt
    assert "이전 세션" in prompt


def test_max_sessions_has_hard_cap():
    assert MAX_SESSIONS > 0
    assert MAX_SESSIONS <= 20  # 합리적 상한


def test_get_rate_limit_wait_no_file():
    """사용량 파일 없으면 (300, False) 반환 — rate limit이 아닌 일반 오류로 처리."""
    with patch("src.overtime.dev_runner._USAGE_FILE", Path("/tmp/nonexistent_file_xyz")):
        wait, is_rl = _get_rate_limit_wait()
        assert is_rl is False


def test_get_rate_limit_wait_low_usage(tmp_path):
    """사용량 80% 미만이면 rate limit이 아닌 다른 오류로 판단."""
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps({
        "five_hour": {"used_percentage": 30, "resets_at": 9999999999},
    }))
    with patch("src.overtime.dev_runner._USAGE_FILE", usage_file):
        wait, is_rl = _get_rate_limit_wait()
        assert is_rl is False


def test_get_rate_limit_wait_high_usage(tmp_path):
    """사용량 80% 이상이면 rate limit으로 판단하고 리셋 시간 기반 대기."""
    import time
    future_reset = int(time.time()) + 600  # 10분 후
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps({
        "five_hour": {"used_percentage": 95, "resets_at": future_reset},
    }))
    with patch("src.overtime.dev_runner._USAGE_FILE", usage_file):
        wait, is_rl = _get_rate_limit_wait()
        assert is_rl is True
        assert 500 <= wait <= 700  # ~600 + 30s buffer, within range
