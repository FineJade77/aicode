package projectconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSetWorkspaceCreatesProjectConfig(t *testing.T) {
	workspace := t.TempDir()

	path, entries, err := SetWorkspace(workspace, "api", "../api")
	if err != nil {
		t.Fatal(err)
	}

	if path != filepath.Join(workspace, ".aicode", "config.json") {
		t.Fatalf("path = %q", path)
	}
	if len(entries) != 1 || entries[0] != (WorkspaceEntry{Name: "api", Path: "../api", Mode: "read_only"}) {
		t.Fatalf("entries = %#v", entries)
	}

	raw := readRawProjectConfig(t, path)
	workspaces := raw["workspaces"].([]any)
	entry := workspaces[0].(map[string]any)
	if entry["name"] != "api" || entry["path"] != "../api" || entry["mode"] != "read_only" {
		t.Fatalf("workspace entry = %#v", entry)
	}
}

func TestSetWorkspaceUpdatesAndPreservesExistingConfig(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	content := `{"commands":{"test":"go test ./..."},"review":{"maxFindings":25},"workspaces":[{"name":"api","path":"../old","mode":"read_only"},{"name":"web","path":"../web","mode":"read_only"}]}`
	if err := os.WriteFile(configPath, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	_, entries, err := SetWorkspace(workspace, "api", "../api")
	if err != nil {
		t.Fatal(err)
	}

	if len(entries) != 2 {
		t.Fatalf("entries = %#v", entries)
	}
	if entries[0] != (WorkspaceEntry{Name: "api", Path: "../api", Mode: "read_only"}) {
		t.Fatalf("api entry = %#v", entries[0])
	}

	raw := readRawProjectConfig(t, configPath)
	if raw["commands"].(map[string]any)["test"] != "go test ./..." {
		t.Fatalf("commands not preserved: %#v", raw)
	}
	if raw["review"].(map[string]any)["maxFindings"].(float64) != 25 {
		t.Fatalf("review not preserved: %#v", raw)
	}
}

func TestRemoveWorkspace(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	content := `{"workspaces":[{"name":"api","path":"../api","mode":"read_only"},{"name":"web","path":"../web","mode":"read_only"}]}`
	if err := os.WriteFile(configPath, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	path, removed, entries, err := RemoveWorkspace(workspace, "api")
	if err != nil {
		t.Fatal(err)
	}

	if path != configPath {
		t.Fatalf("path = %q", path)
	}
	if !removed {
		t.Fatal("expected removed")
	}
	if len(entries) != 1 || entries[0].Name != "web" {
		t.Fatalf("entries = %#v", entries)
	}
}

func TestRemoveWorkspaceReportsMissing(t *testing.T) {
	workspace := t.TempDir()

	path, removed, entries, err := RemoveWorkspace(workspace, "api")
	if err != nil {
		t.Fatal(err)
	}

	if path != filepath.Join(workspace, ".aicode", "config.json") {
		t.Fatalf("path = %q", path)
	}
	if removed {
		t.Fatal("expected missing workspace")
	}
	if len(entries) != 0 {
		t.Fatalf("entries = %#v", entries)
	}
}

func TestSetWorkspaceRejectsInvalidName(t *testing.T) {
	workspace := t.TempDir()

	_, _, err := SetWorkspace(workspace, "api:prod", "../api")
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "workspace name") {
		t.Fatalf("error = %v", err)
	}
}

func readRawProjectConfig(t *testing.T, path string) map[string]any {
	t.Helper()
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(content, &raw); err != nil {
		t.Fatal(err)
	}
	return raw
}
