import json
from pathlib import Path

from app.project.config import load_project_config, parse_project_config
from app.project.detect import detect_test_command


def test_parse_project_config() -> None:
    config = parse_project_config(
        {
            "projectName": "demo",
            "defaultLanguage": "en-US",
            "commands": {"test": "python3 -m pytest tests/unit"},
            "protectedPaths": [".env", "secret/**"],
            "workspaces": [{"name": "api", "path": "../api", "mode": "read_only"}],
            "review": {
                "disabledRules": ["large_diff"],
                "largeDiffThreshold": 1200,
                "maxFindings": 25,
            },
        }
    )

    assert config.project_name == "demo"
    assert config.default_language == "en-US"
    assert config.commands["test"] == "python3 -m pytest tests/unit"
    assert config.protected_paths == [".env", "secret/**"]
    assert config.workspaces[0].name == "api"
    assert config.review.disabled_rules == ["large_diff"]
    assert config.review.large_diff_threshold == 1200
    assert config.review.max_findings == 25


def test_load_project_config_defaults_when_missing(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)

    assert ".env" in config.protected_paths
    assert config.commands == {}
    assert config.review.disabled_rules == []
    assert config.review.large_diff_threshold == 500
    assert config.review.max_findings == 50


def test_parse_project_config_bounds_review_values() -> None:
    config = parse_project_config({"review": {"largeDiffThreshold": 1, "maxFindings": 1000}})

    assert config.review.large_diff_threshold == 50
    assert config.review.max_findings == 500


def test_detect_test_command_uses_project_config_override(tmp_path: Path) -> None:
    write_project_config(tmp_path, {"commands": {"test": "python3 -m pytest tests/unit"}})
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./cli\n", encoding="utf-8")

    assert detect_test_command(tmp_path) == "python3 -m pytest tests/unit"


def write_project_config(tmp_path: Path, data: dict) -> None:
    config_dir = tmp_path / ".aicode"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")
