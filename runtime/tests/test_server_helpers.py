from pathlib import Path

from app.project.detect import detect_test_command
from app.server.main import detect_append_request


def test_detect_test_command_for_go_work(tmp_path: Path) -> None:
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    assert detect_test_command(tmp_path) == "go test ./cli/..."


def test_detect_append_request() -> None:
    assert detect_append_request("append README.md hello world") == ("README.md", "hello world")
    assert detect_append_request("追加 README.md 你好") == ("README.md", "你好")
