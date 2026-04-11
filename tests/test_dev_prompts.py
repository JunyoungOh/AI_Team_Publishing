"""개발 모드 프롬프트 빌드 테스트."""
from src.overtime.dev_prompts import (
    build_clarify_prompt,
    build_dev_system_prompt,
    build_handoff_prompt,
    build_report_prompt,
)


def test_build_clarify_prompt():
    system, user = build_clarify_prompt("할일 관리 앱 만들어줘")
    assert "할일 관리 앱" in user
    assert "질문" in system
    assert "비개발자" in system


def test_build_dev_system_prompt_contains_all_phases():
    prompt = build_dev_system_prompt("test app", "answers", "/tmp/t")
    for phase in ["Phase 1", "Phase 2", "Phase 3", "Phase 4", "Phase 5"]:
        assert phase in prompt


def test_build_dev_system_prompt_enforces_local():
    prompt = build_dev_system_prompt("test", "a", "/tmp/t")
    assert "로컬" in prompt


def test_build_dev_system_prompt_no_mcp_tools():
    prompt = build_dev_system_prompt("test", "a", "/tmp/t")
    assert "mcp__" not in prompt


def test_build_dev_system_prompt_has_completion_marker():
    prompt = build_dev_system_prompt("test", "a", "/tmp/t")
    assert "ALL_PHASES_DONE" in prompt


def test_build_dev_system_prompt_with_handoff():
    prompt = build_dev_system_prompt(
        "test app", "Q1: web", "/tmp/t",
        handoff_context="Phase 3 진행 중. 파일: app.py, index.html",
    )
    assert "이전 세션" in prompt
    assert "Phase 3 진행 중" in prompt


def test_build_handoff_prompt():
    system, user = build_handoff_prompt("/tmp/test-app")
    assert "/tmp/test-app" in user
    assert "진행 상황" in system


def test_build_report_prompt():
    system, user = build_report_prompt("할일 앱", "/tmp/app", "/tmp/report")
    assert "실행 방법" in system
    assert "/tmp/report" in user
    assert "할일 앱" in user
