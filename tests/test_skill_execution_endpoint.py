"""스킬 실행 엔드포인트 통합 테스트.

WS는 run_skill 함수를 모킹하여 와이어링만 검증.
REST는 임시 runs_root에 fixture 데이터를 넣고 응답 검증.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.skill_builder.run_history import save_run
from src.ui.server import app


def test_runs_endpoint_returns_history(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.skill_builder.run_history._DEFAULT_RUNS_ROOT", tmp_path
    )
    save_run(
        slug="summarize",
        user_input="긴 글",
        result_text="짧은 글",
        status="completed",
        tool_count=3,
        duration_seconds=10.0,
        runs_root=tmp_path,
    )
    client = TestClient(app)
    res = client.get("/api/skill-builder/runs/summarize")
    assert res.status_code == 200
    body = res.json()
    assert "runs" in body
    assert len(body["runs"]) == 1
    assert body["runs"][0]["slug"] == "summarize"


def test_runs_endpoint_empty_for_unknown_slug(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.skill_builder.run_history._DEFAULT_RUNS_ROOT", tmp_path
    )
    client = TestClient(app)
    res = client.get("/api/skill-builder/runs/never-ran")
    assert res.status_code == 200
    assert res.json() == {"runs": []}


def test_runs_endpoint_rejects_path_traversal() -> None:
    client = TestClient(app)
    res = client.get("/api/skill-builder/runs/..%2Fetc")
    # FastAPI는 보통 URL 디코딩 후 라우팅 — 400 또는 404 모두 OK
    assert res.status_code in (400, 404, 422)


def test_execute_ws_completed_flow(tmp_path: Path, monkeypatch) -> None:
    """WebSocket: execute 메시지 → started → tool_use → completed 흐름."""
    from src.skill_builder.run_history import RunRecord

    fake_record = RunRecord(
        run_id="123-abc",
        slug="summarize",
        user_input="x",
        result_text="결과",
        status="completed",
        tool_count=2,
        duration_seconds=1.0,
        started_at="2026-04-09T00:00:00+00:00",
    )

    async def fake_run(*, slug, user_input, on_event, **kwargs):
        on_event({"action": "started", "elapsed": 0})
        on_event({
            "action": "tool_use",
            "tool": "Read",
            "tool_count": 1,
            "elapsed": 0.5,
        })
        on_event({
            "action": "completed",
            "elapsed": 1.0,
            "tool_count": 2,
            "timed_out": False,
        })
        return fake_record

    # CRITICAL: server.py imports run_skill INSIDE the endpoint function,
    # so we must patch the source module (execution_runner) not ui.server.
    # Patching src.ui.server.run_skill would silently no-op.
    with patch(
        "src.skill_builder.execution_runner.run_skill",
        side_effect=fake_run,
    ):
        client = TestClient(app)
        with client.websocket_connect("/ws/skill-execute") as ws:
            ws.send_json({
                "type": "execute",
                "data": {"slug": "summarize", "user_input": "x"},
            })
            messages = []
            for _ in range(20):
                try:
                    messages.append(ws.receive_json())
                except Exception:
                    break
                if messages[-1]["type"] in ("completed", "error"):
                    break

    types = [m["type"] for m in messages]
    assert "started" in types
    assert "tool_use" in types
    assert "completed" in types
    completed_msg = next(m for m in messages if m["type"] == "completed")
    assert completed_msg["data"]["run_id"] == "123-abc"
    assert completed_msg["data"]["result_text"] == "결과"
