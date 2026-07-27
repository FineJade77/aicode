package projectconfig

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSetTestCommandCreatesProjectConfig(t *testing.T) {
	workspace := t.TempDir()

	path, command, err := SetTestCommand(workspace, "python3 -m pytest tests/unit")
	if err != nil {
		t.Fatal(err)
	}

	if path != filepath.Join(workspace, ".aicode", "config.json") {
		t.Fatalf("path = %q", path)
	}
	if command != "python3 -m pytest tests/unit" {
		t.Fatalf("command = %q", command)
	}
	raw := readRawProjectConfig(t, path)
	if raw["commands"].(map[string]any)["test"] != "python3 -m pytest tests/unit" {
		t.Fatalf("config = %#v", raw)
	}
}

func TestSetTestCommandPreservesExistingConfig(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	content := `{"review":{"maxFindings":25},"workspaces":[{"name":"api","path":"../api","mode":"read_only"}]}`
	if err := os.WriteFile(configPath, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	_, _, err := SetTestCommand(workspace, "go test ./...")
	if err != nil {
		t.Fatal(err)
	}

	raw := readRawProjectConfig(t, configPath)
	if raw["review"].(map[string]any)["maxFindings"].(float64) != 25 {
		t.Fatalf("review not preserved: %#v", raw)
	}
	if raw["workspaces"].([]any)[0].(map[string]any)["name"] != "api" {
		t.Fatalf("workspaces not preserved: %#v", raw)
	}
}

func TestGetTestCommand(t *testing.T) {
	workspace := t.TempDir()
	_, _, err := SetTestCommand(workspace, "auto")
	if err != nil {
		t.Fatal(err)
	}

	_, command, configured, err := GetTestCommand(workspace)
	if err != nil {
		t.Fatal(err)
	}

	if !configured || command != "auto" {
		t.Fatalf("configured = %v command = %q", configured, command)
	}
}

func TestGetCommandReadsNamedCommand(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	if err := os.WriteFile(configPath, []byte(`{"commands":{"build":"npm run build","lint":"ruff check ."}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	path, command, configured, err := GetCommand(workspace, "lint")
	if err != nil {
		t.Fatal(err)
	}

	if path != configPath {
		t.Fatalf("path = %q", path)
	}
	if !configured || command != "ruff check ." {
		t.Fatalf("configured = %v command = %q", configured, command)
	}
}

func TestGetTestCommandReportsMissing(t *testing.T) {
	workspace := t.TempDir()

	_, command, configured, err := GetTestCommand(workspace)
	if err != nil {
		t.Fatal(err)
	}

	if configured || command != "" {
		t.Fatalf("configured = %v command = %q", configured, command)
	}
}

func TestUnsetTestCommand(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	content := `{"commands":{"test":"go test ./...","lint":"golangci-lint run"},"review":{"maxFindings":25}}`
	if err := os.WriteFile(configPath, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	path, removed, err := UnsetTestCommand(workspace)
	if err != nil {
		t.Fatal(err)
	}

	if path != configPath {
		t.Fatalf("path = %q", path)
	}
	if !removed {
		t.Fatal("expected removed")
	}
	raw := readRawProjectConfig(t, configPath)
	commands := raw["commands"].(map[string]any)
	if _, ok := commands["test"]; ok {
		t.Fatalf("test command still present: %#v", commands)
	}
	if commands["lint"] != "golangci-lint run" {
		t.Fatalf("other command not preserved: %#v", commands)
	}
	if raw["review"].(map[string]any)["maxFindings"].(float64) != 25 {
		t.Fatalf("review not preserved: %#v", raw)
	}
}

func TestSetTestCommandRejectsEmpty(t *testing.T) {
	workspace := t.TempDir()

	_, _, err := SetTestCommand(workspace, " ")
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "must not be empty") {
		t.Fatalf("error = %v", err)
	}
}
