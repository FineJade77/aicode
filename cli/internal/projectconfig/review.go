package projectconfig

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func SetReviewRuleDisabled(workspacePath string, rule string, disabled bool) (string, []string, error) {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return "", nil, errors.New("rule id 不能为空")
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, nil, err
	}

	review := objectValue(raw["review"])
	rules := stringList(review["disabledRules"])
	if disabled {
		rules = appendUnique(rules, rule)
	} else {
		rules = removeValue(rules, rule)
	}
	sort.Strings(rules)

	review["disabledRules"] = rules
	raw["review"] = review

	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return path, nil, err
	}
	encoded, err := json.MarshalIndent(raw, "", "  ")
	if err != nil {
		return path, nil, err
	}
	encoded = append(encoded, '\n')
	return path, rules, os.WriteFile(path, encoded, 0o644)
}

func readProjectConfig(path string) (map[string]any, error) {
	content, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, err
	}
	if len(strings.TrimSpace(string(content))) == 0 {
		return map[string]any{}, nil
	}

	var raw map[string]any
	if err := json.Unmarshal(content, &raw); err != nil {
		return nil, fmt.Errorf("项目配置不是合法 JSON: %w", err)
	}
	return raw, nil
}

func objectValue(value any) map[string]any {
	if obj, ok := value.(map[string]any); ok {
		return obj
	}
	return map[string]any{}
}

func stringList(value any) []string {
	raw, ok := value.([]any)
	if !ok {
		return []string{}
	}
	values := make([]string, 0, len(raw))
	seen := map[string]bool{}
	for _, item := range raw {
		text, ok := item.(string)
		if !ok || seen[text] {
			continue
		}
		seen[text] = true
		values = append(values, text)
	}
	return values
}

func appendUnique(values []string, value string) []string {
	for _, existing := range values {
		if existing == value {
			return values
		}
	}
	return append(values, value)
}

func removeValue(values []string, value string) []string {
	next := values[:0]
	for _, existing := range values {
		if existing != value {
			next = append(next, existing)
		}
	}
	return next
}
