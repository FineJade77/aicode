package projectconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestSetReviewRuleDisabledCreatesProjectConfig(t *testing.T) {
	workspace := t.TempDir()

	path, rules, err := SetReviewRuleDisabled(workspace, "large_diff", true)
	if err != nil {
		t.Fatal(err)
	}

	if path != filepath.Join(workspace, ".aicode", "config.json") {
		t.Fatalf("path = %q", path)
	}
	if len(rules) != 1 || rules[0] != "large_diff" {
		t.Fatalf("rules = %#v", rules)
	}

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(content, &raw); err != nil {
		t.Fatal(err)
	}
	review := raw["review"].(map[string]any)
	disabled := review["disabledRules"].([]any)
	if disabled[0] != "large_diff" {
		t.Fatalf("disabledRules = %#v", disabled)
	}
}

func TestSetReviewRuleDisabledPreservesExistingConfig(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	if err := os.WriteFile(configPath, []byte(`{"commands":{"test":"go test ./..."},"review":{"disabledRules":["debug_output"],"maxFindings":25}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	_, rules, err := SetReviewRuleDisabled(workspace, "large_diff", true)
	if err != nil {
		t.Fatal(err)
	}

	if len(rules) != 2 || rules[0] != "debug_output" || rules[1] != "large_diff" {
		t.Fatalf("rules = %#v", rules)
	}

	content, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(content, &raw); err != nil {
		t.Fatal(err)
	}
	if raw["commands"].(map[string]any)["test"] != "go test ./..." {
		t.Fatalf("commands not preserved: %#v", raw)
	}
	if raw["review"].(map[string]any)["maxFindings"].(float64) != 25 {
		t.Fatalf("review maxFindings not preserved: %#v", raw)
	}
}

func TestSetReviewRuleEnabledRemovesRule(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configDir, "config.json"), []byte(`{"review":{"disabledRules":["debug_output","large_diff"]}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	_, rules, err := SetReviewRuleDisabled(workspace, "large_diff", false)
	if err != nil {
		t.Fatal(err)
	}

	if len(rules) != 1 || rules[0] != "debug_output" {
		t.Fatalf("rules = %#v", rules)
	}
}
