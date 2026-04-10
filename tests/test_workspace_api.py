"""워크스페이스 API 테스트."""
from unittest.mock import patch
from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def ws_base(tmp_path):
    return tmp_path


@pytest.fixture
def client(ws_base):
    with patch("src.ui.routes.workspace.WS_BASE", ws_base):
        with patch("src.utils.workspace._DEFAULT_BASE", ws_base):
            from src.ui.server import app
            yield TestClient(app)


def test_list_files_empty(client, ws_base):
    (ws_base / "instant" / "input").mkdir(parents=True, exist_ok=True)
    resp = client.get("/api/workspace/instant/files")
    assert resp.status_code == 200
    assert resp.json()["files"] == []


def test_list_files_with_content(client, ws_base):
    inp = ws_base / "instant" / "input"
    inp.mkdir(parents=True, exist_ok=True)
    (inp / "data.csv").write_text("a,b")
    resp = client.get("/api/workspace/instant/files")
    assert len(resp.json()["files"]) == 1


def test_list_files_invalid_mode(client):
    resp = client.get("/api/workspace/invalid_mode/files")
    assert resp.status_code == 400
