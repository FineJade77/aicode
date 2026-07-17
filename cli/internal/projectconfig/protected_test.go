package projectconfig

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestListProtectedPathsUsesDefaultsWhenMissing(t *testing.T) {
	workspace := t.TempDir()

	_, values, explicit, err := ListProtectedPaths(workspace)
	if err != nil {
		t.Fatal(err)
	}

	if explicit {
		t.Fatal("expected default protected paths")
	}
	assertStringSet(t, values, DefaultProtectedPaths())
}

func TestAddProtectedPathPreservesDefaults(t *testing.T) {
	workspace := t.TempDir()

	path, values, err := AddProtectedPath(workspace, "private/**")
	if err != nil {
		t.Fatal(err)
	}

	assertStringSet(t, values, append(DefaultProtectedPaths(), "private/**"))
	raw := readRawProjectConfig(t, path)
	assertStringSet(t, stringList(raw["protectedPaths"]), append(DefaultProtectedPaths(), "private/**"))
}

func TestAddProtectedPathPreservesExistingConfig(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	content := `{"commands":{"test":"go test ./..."},"protectedPaths":["secret/**"]}`
	if err := os.WriteFile(configPath, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	_, values, err := AddProtectedPath(workspace, ".env")
	if err != nil {
		t.Fatal(err)
	}

	assertStringSet(t, values, []string{".env", "secret/**"})
	raw := readRawProjectConfig(t, configPath)
	if raw["commands"].(map[string]any)["test"] != "go test ./..." {
		t.Fatalf("commands not preserved: %#v", raw)
	}
}

func TestRemoveProtectedPathCanOverrideDefault(t *testing.T) {
	workspace := t.TempDir()

	path, removed, values, err := RemoveProtectedPath(workspace, "secrets/**")
	if err != nil {
		t.Fatal(err)
	}

	if !removed {
		t.Fatal("expected removed")
	}
	assertStringSet(t, values, []string{".env", ".env.*", "infra/prod/**"})
	raw := readRawProjectConfig(t, path)
	assertStringSet(t, stringList(raw["protectedPaths"]), []string{".env", ".env.*", "infra/prod/**"})
}

func TestResetProtectedPathsDeletesOverride(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	content := `{"protectedPaths":["private/**"],"review":{"maxFindings":25}}`
	if err := os.WriteFile(configPath, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	path, values, err := ResetProtectedPaths(workspace)
	if err != nil {
		t.Fatal(err)
	}

	if path != configPath {
		t.Fatalf("path = %q", path)
	}
	assertStringSet(t, values, DefaultProtectedPaths())
	raw := readRawProjectConfig(t, configPath)
	if _, ok := raw["protectedPaths"]; ok {
		t.Fatalf("protectedPaths still present: %#v", raw)
	}
	if raw["review"].(map[string]any)["maxFindings"].(float64) != 25 {
		t.Fatalf("review not preserved: %#v", raw)
	}
}

func TestAddProtectedPathRejectsEmpty(t *testing.T) {
	workspace := t.TempDir()

	_, _, err := AddProtectedPath(workspace, " ")
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "不能为空") {
		t.Fatalf("error = %v", err)
	}
}

func assertStringSet(t *testing.T, got []string, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("got = %#v, want = %#v", got, want)
	}
	seen := map[string]bool{}
	for _, value := range got {
		seen[value] = true
	}
	for _, value := range want {
		if !seen[value] {
			t.Fatalf("got = %#v, missing %q", got, value)
		}
	}
}
