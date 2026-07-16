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

func SetReviewRuleDisabled(workspacePath string, rule string, disabled bool, knownRules []string) (string, []string, error) {
	rule = strings.TrimSpace(rule)
	if rule == "" {
		return "", nil, errors.New("rule id 不能为空")
	}
	if !knownRuleSet(knownRules)[rule] {
		return "", nil, fmt.Errorf("未知 review 规则: %s。运行 aicode review-rules 查看支持列表", rule)
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

	return path, rules, writeProjectConfig(path, raw)
}

func PruneUnknownReviewRules(workspacePath string, knownRules []string) (string, []string, []string, error) {
	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, nil, nil, err
	}

	review := objectValue(raw["review"])
	rules := stringList(review["disabledRules"])
	known := knownRuleSet(knownRules)
	kept := make([]string, 0, len(rules))
	removed := make([]string, 0)
	for _, rule := range rules {
		if known[rule] {
			kept = append(kept, rule)
			continue
		}
		removed = append(removed, rule)
	}
	sort.Strings(kept)
	sort.Strings(removed)

	if len(removed) == 0 {
		return path, removed, kept, nil
	}

	review["disabledRules"] = kept
	raw["review"] = review
	return path, removed, kept, writeProjectConfig(path, raw)
}

func SetReviewNumber(workspacePath string, key string, value int) (string, string, int, error) {
	field, minValue, maxValue, err := reviewNumberField(key)
	if err != nil {
		return "", "", 0, err
	}
	if value < minValue || value > maxValue {
		return "", "", 0, fmt.Errorf("%s 必须在 %d 到 %d 之间", key, minValue, maxValue)
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, field, value, err
	}

	review := objectValue(raw["review"])
	review[field] = value
	raw["review"] = review
	if err := writeProjectConfig(path, raw); err != nil {
		return path, field, value, err
	}
	return path, field, value, nil
}

func UnsetReviewNumber(workspacePath string, key string) (string, string, error) {
	field, _, _, err := reviewNumberField(key)
	if err != nil {
		return "", "", err
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, field, err
	}

	review := objectValue(raw["review"])
	delete(review, field)
	raw["review"] = review
	if err := writeProjectConfig(path, raw); err != nil {
		return path, field, err
	}
	return path, field, nil
}

func reviewNumberField(key string) (string, int, int, error) {
	switch strings.TrimSpace(key) {
	case "largeDiffThreshold":
		return "largeDiffThreshold", 50, 50_000, nil
	case "maxFindings":
		return "maxFindings", 1, 500, nil
	default:
		return "", 0, 0, fmt.Errorf("未知 review 配置项: %s。支持 largeDiffThreshold 或 maxFindings", key)
	}
}

func knownRuleSet(knownRules []string) map[string]bool {
	known := make(map[string]bool, len(knownRules))
	for _, rule := range knownRules {
		rule = strings.TrimSpace(rule)
		if rule != "" {
			known[rule] = true
		}
	}
	return known
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

func writeProjectConfig(path string, raw map[string]any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	encoded, err := json.MarshalIndent(raw, "", "  ")
	if err != nil {
		return err
	}
	encoded = append(encoded, '\n')
	return os.WriteFile(path, encoded, 0o644)
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
