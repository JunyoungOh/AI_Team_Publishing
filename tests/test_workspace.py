"""워크스페이스 유틸 테스트."""
from pathlib import Path
from src.utils.workspace import (
    ensure_workspace, list_input_files, read_files_as_context, get_output_dir,
)


def test_ensure_workspace_creates_dirs(tmp_path):
    ensure_workspace("builder", base=tmp_path)
    assert (tmp_path / "builder" / "input").is_dir()
    assert (tmp_path / "builder" / "output").is_dir()


def test_list_input_files(tmp_path):
    ensure_workspace("builder", base=tmp_path)
    inp = tmp_path / "builder" / "input"
    (inp / "data.csv").write_text("a,b\n1,2")
    (inp / "notes.txt").write_text("hello")
    files = list_input_files("builder", base=tmp_path)
    names = [f["name"] for f in files]
    assert "data.csv" in names
    assert "notes.txt" in names


def test_list_input_files_empty(tmp_path):
    ensure_workspace("builder", base=tmp_path)
    assert list_input_files("builder", base=tmp_path) == []


def test_read_files_as_context_text(tmp_path):
    ensure_workspace("builder", base=tmp_path)
    (tmp_path / "builder" / "input" / "report.csv").write_text("name,value\nfoo,42")
    ctx = read_files_as_context("builder", ["report.csv"], base=tmp_path)
    assert "report.csv" in ctx
    assert "foo,42" in ctx


def test_read_files_as_context_binary(tmp_path):
    ensure_workspace("builder", base=tmp_path)
    (tmp_path / "builder" / "input" / "img.png").write_bytes(b"\x89PNG\r\n")
    ctx = read_files_as_context("builder", ["img.png"], base=tmp_path)
    assert "이미지" in ctx


def test_read_files_as_context_missing_file(tmp_path):
    ensure_workspace("builder", base=tmp_path)
    assert read_files_as_context("builder", ["nope.txt"], base=tmp_path) == ""


def test_read_files_as_context_path_traversal(tmp_path):
    ensure_workspace("builder", base=tmp_path)
    assert read_files_as_context("builder", ["../../etc/passwd"], base=tmp_path) == ""


def test_read_files_as_context_empty_list(tmp_path):
    assert read_files_as_context("builder", [], base=tmp_path) == ""


def test_get_output_dir(tmp_path):
    out = get_output_dir("builder", "abc123", base=tmp_path)
    assert out.is_dir()
    assert "abc123" in str(out)


def test_e2e_file_to_context(tmp_path):
    """파일 넣기 → list → read_files_as_context 전체 파이프라인."""
    from src.utils.workspace import ensure_workspace, list_input_files, read_files_as_context
    ensure_workspace("builder", base=tmp_path)
    inp = tmp_path / "builder" / "input"
    (inp / "sales.csv").write_text("month,revenue\nJan,100\nFeb,200")
    (inp / "memo.txt").write_text("Q1 분석 요청")

    files = list_input_files("builder", base=tmp_path)
    assert len(files) == 2

    names = [f["name"] for f in files]
    ctx = read_files_as_context("builder", names, base=tmp_path)
    assert "sales.csv" in ctx
    assert "Jan,100" in ctx
    assert "Q1 분석 요청" in ctx


def test_backward_compat_no_files(tmp_path):
    """파일 미선택 시 빈 문자열 → 기존 동작과 동일."""
    from src.utils.workspace import read_files_as_context
    ctx = read_files_as_context("builder", [], base=tmp_path)
    assert ctx == ""


def test_output_dir_creation_and_write(tmp_path):
    """output 디렉토리 생성 + 파일 쓰기 가능 확인."""
    from src.utils.workspace import get_output_dir
    out = get_output_dir("builder", "sess-001", base=tmp_path)
    assert out.is_dir()
    (out / "result.html").write_text("<h1>Done</h1>")
    assert (out / "result.html").read_text() == "<h1>Done</h1>"
