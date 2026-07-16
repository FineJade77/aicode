package projectconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

var testKnownReviewRules = []string{"debug_output", "large_diff", "risky_inner_html", "secret_added"}

func TestSetReviewRuleDisabledCreatesProjectConfig(t *testing.T) {
	workspace := t.TempDir()

	path, rules, err := SetReviewRuleDisabled(workspace, "large_diff", true, testKnownReviewRules)
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

	_, rules, err := SetReviewRuleDisabled(workspace, "large_diff", true, testKnownReviewRules)
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

	_, rules, err := SetReviewRuleDisabled(workspace, "large_diff", false, testKnownReviewRules)
	if err != nil {
		t.Fatal(err)
	}

	if len(rules) != 1 || rules[0] != "debug_output" {
		t.Fatalf("rules = %#v", rules)
	}
}

func TestSetReviewRuleDisabledRejectsUnknownRule(t *testing.T) {
	workspace := t.TempDir()

	path, rules, err := SetReviewRuleDisabled(workspace, "not_a_rule", true, testKnownReviewRules)
	if err == nil {
		t.Fatal("expected error")
	}
	if path != "" {
		t.Fatalf("path = %q", path)
	}
	if rules != nil {
		t.Fatalf("rules = %#v", rules)
	}
	if !strings.Contains(err.Error(), "未知 review 规则") {
		t.Fatalf("error = %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(workspace, ".aicode", "config.json")); !os.IsNotExist(statErr) {
		t.Fatalf("expected no config file, stat err = %v", statErr)
	}
}

func TestSetReviewRuleDisabledUsesProvidedKnownRules(t *testing.T) {
	workspace := t.TempDir()

	_, rules, err := SetReviewRuleDisabled(workspace, "runtime_only_rule", true, []string{"runtime_only_rule"})
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 1 || rules[0] != "runtime_only_rule" {
		t.Fatalf("rules = %#v", rules)
	}
}

func TestPruneUnknownReviewRules(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	if err := os.WriteFile(configPath, []byte(`{"commands":{"test":"go test ./..."},"review":{"disabledRules":["old_rule","large_diff","debug_output"],"maxFindings":25}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	path, removed, rules, err := PruneUnknownReviewRules(workspace, testKnownReviewRules)
	if err != nil {
		t.Fatal(err)
	}

	if path != configPath {
		t.Fatalf("path = %q", path)
	}
	if len(removed) != 1 || removed[0] != "old_rule" {
		t.Fatalf("removed = %#v", removed)
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
		t.Fatalf("maxFindings not preserved: %#v", raw)
	}
	disabled := raw["review"].(map[string]any)["disabledRules"].([]any)
	if len(disabled) != 2 || disabled[0] != "debug_output" || disabled[1] != "large_diff" {
		t.Fatalf("disabledRules = %#v", disabled)
	}
}

func TestPruneUnknownReviewRulesNoopWhenConfigMissing(t *testing.T) {
	workspace := t.TempDir()

	path, removed, rules, err := PruneUnknownReviewRules(workspace, testKnownReviewRules)
	if err != nil {
		t.Fatal(err)
	}

	if path != filepath.Join(workspace, ".aicode", "config.json") {
		t.Fatalf("path = %q", path)
	}
	if len(removed) != 0 || len(rules) != 0 {
		t.Fatalf("removed = %#v rules = %#v", removed, rules)
	}
	if _, statErr := os.Stat(path); !os.IsNotExist(statErr) {
		t.Fatalf("expected no config file, stat err = %v", statErr)
	}
}

func TestSetReviewNumber(t *testing.T) {
	workspace := t.TempDir()

	path, field, value, err := SetReviewNumber(workspace, "largeDiffThreshold", 1200)
	if err != nil {
		t.Fatal(err)
	}

	if field != "largeDiffThreshold" || value != 1200 {
		t.Fatalf("field = %q value = %d", field, value)
	}
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(content, &raw); err != nil {
		t.Fatal(err)
	}
	if raw["review"].(map[string]any)["largeDiffThreshold"].(float64) != 1200 {
		t.Fatalf("config = %#v", raw)
	}
}

func TestSetReviewNumberRejectsInvalidValue(t *testing.T) {
	workspace := t.TempDir()

	path, _, _, err := SetReviewNumber(workspace, "largeDiffThreshold", 10)
	if err == nil {
		t.Fatal("expected error")
	}
	if path != "" {
		t.Fatalf("path = %q", path)
	}
	if !strings.Contains(err.Error(), "必须在 50 到 50000 之间") {
		t.Fatalf("error = %v", err)
	}
}

func TestUnsetReviewNumber(t *testing.T) {
	workspace := t.TempDir()
	configDir := filepath.Join(workspace, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(configDir, "config.json")
	if err := os.WriteFile(configPath, []byte(`{"review":{"disabledRules":["large_diff"],"largeDiffThreshold":1200,"maxFindings":25}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	path, field, err := UnsetReviewNumber(workspace, "largeDiffThreshold")
	if err != nil {
		t.Fatal(err)
	}
	if path != configPath || field != "largeDiffThreshold" {
		t.Fatalf("path = %q field = %q", path, field)
	}

	content, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(content, &raw); err != nil {
		t.Fatal(err)
	}
	review := raw["review"].(map[string]any)
	if _, ok := review["largeDiffThreshold"]; ok {
		t.Fatalf("largeDiffThreshold still present: %#v", review)
	}
	if review["maxFindings"].(float64) != 25 {
		t.Fatalf("maxFindings not preserved: %#v", review)
	}
	if review["disabledRules"].([]any)[0] != "large_diff" {
		t.Fatalf("disabledRules not preserved: %#v", review)
	}
}
