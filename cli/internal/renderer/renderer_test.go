package renderer

import (
	"strings"
	"testing"
)

func TestUsageLineIncludesPurpose(t *testing.T) {
	line := usageLine(map[string]any{
		"purpose":        "reviewer",
		"model":          "stub",
		"input_tokens":   12,
		"output_tokens":  3,
		"estimated_cost": 0.00012345,
	})

	want := "用量: purpose=reviewer model=stub input=12 output=3 cost=$0.00012345"
	if line != want {
		t.Fatalf("usageLine() = %q, want %q", line, want)
	}
}

func TestUsageLineDefaultsMissingPurpose(t *testing.T) {
	line := usageLine(map[string]any{
		"model":         "stub",
		"input_tokens":  12,
		"output_tokens": 3,
	})

	want := "用量: purpose=unknown model=stub input=12 output=3 cost=$0"
	if line != want {
		t.Fatalf("usageLine() = %q, want %q", line, want)
	}
}

func TestReviewRulesTable(t *testing.T) {
	table := ReviewRulesTable(reviewRulesFixture())

	assertContains(t, table, "Review 配置")
	assertContains(t, table, "disabledRules: large_diff, old_rule")
	assertContains(t, table, "largeDiffThreshold: 1200")
	assertContains(t, table, "Warnings")
	assertContains(t, table, "old_rule: disabledRules 包含未知规则 old_rule，该配置不会生效。")
	assertContains(t, table, "disabled")
	assertContains(t, table, "large_diff")
	assertContains(t, table, "enabled")
	assertContains(t, table, "secret_added")
}

func TestReviewRulesMarkdown(t *testing.T) {
	doc := ReviewRulesMarkdown(reviewRulesFixture())

	assertContains(t, doc, "# aicode Review Rules")
	assertContains(t, doc, "- disabledRules: `large_diff, old_rule`")
	assertContains(t, doc, "## Config Warnings")
	assertContains(t, doc, "| State | Severity | Rule | Description |")
	assertContains(t, doc, "| disabled | medium | `large_diff` | 当前 diff 超过阈值 |")
	assertContains(t, doc, "| enabled | high | `secret_added` | 新增内容匹配凭证 |")
}

func reviewRulesFixture() map[string]any {
	return map[string]any{
		"effective_config": map[string]any{
			"disabled_rules":       []any{"large_diff", "old_rule"},
			"large_diff_threshold": float64(1200),
			"max_findings":         float64(25),
		},
		"config_warnings": []any{
			map[string]any{
				"rule":    "old_rule",
				"message": "disabledRules 包含未知规则 old_rule，该配置不会生效。",
			},
		},
		"rules": []any{
			map[string]any{
				"id":          "large_diff",
				"severity":    "medium",
				"enabled":     false,
				"title":       "diff 规模较大",
				"description": "当前 diff 超过阈值",
			},
			map[string]any{
				"id":          "secret_added",
				"severity":    "high",
				"enabled":     true,
				"title":       "新增行包含疑似密钥",
				"description": "新增内容匹配凭证",
			},
		},
	}
}

func assertContains(t *testing.T, value string, needle string) {
	t.Helper()
	if !strings.Contains(value, needle) {
		t.Fatalf("%q does not contain %q", value, needle)
	}
}
