package renderer

import (
	"io"
	"os"
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

func TestRenderEventPrintsRunStatus(t *testing.T) {
	output := captureRenderEvent(map[string]any{
		"type":    "run.queued",
		"message": "任务已排队，等待当前会话中的上一条任务完成。",
	})

	assertContains(t, output, "任务已排队")
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

func TestModelRoutesTable(t *testing.T) {
	table := ModelRoutesTable(map[string]any{
		"provider": map[string]any{
			"primary":            "openai_compatible",
			"primary_configured": false,
			"fallback":           "stub",
		},
		"routes": map[string]any{
			"reviewer":   "gpt-5",
			"summarizer": "gpt-5-mini",
		},
		"openai_compatible": map[string]any{
			"base_url":        "https://api.example.com/v1",
			"api_key_env":     "OPENAI_API_KEY",
			"timeout_seconds": float64(12.5),
		},
		"pricing": map[string]any{
			"currency": "USD",
			"unit":     "per_1m_tokens",
			"models": []any{
				map[string]any{
					"provider":      "openai_compatible",
					"model":         "gpt-5",
					"input_per_1m":  float64(1.25),
					"output_per_1m": float64(10),
				},
			},
		},
	})

	assertContains(t, table, "Model Routes")
	assertContains(t, table, "primary: openai_compatible (configured: false)")
	assertContains(t, table, "reviewer")
	assertContains(t, table, "gpt-5")
	assertContains(t, table, "OpenAI-compatible")
	assertContains(t, table, "base_url: https://api.example.com/v1")
	assertContains(t, table, "Pricing (USD / per_1m_tokens)")
	assertContains(t, table, "openai_compatible")
}

func TestUsageSummaryTable(t *testing.T) {
	table := UsageSummaryTable(map[string]any{
		"record_count":        float64(2),
		"total_input_tokens":  float64(30),
		"total_output_tokens": float64(15),
		"total_tokens":        float64(45),
		"estimated_cost":      float64(0.03),
		"audit_path":          "/tmp/audit.jsonl",
		"filters": map[string]any{
			"session_id": "sess_1",
			"date":       "2026-07-16",
		},
		"by_purpose": map[string]any{
			"summarizer": map[string]any{
				"record_count":   float64(1),
				"input_tokens":   float64(10),
				"output_tokens":  float64(5),
				"total_tokens":   float64(15),
				"estimated_cost": float64(0.01),
			},
		},
		"by_model": map[string]any{
			"stub": map[string]any{
				"record_count":   float64(1),
				"input_tokens":   float64(10),
				"output_tokens":  float64(5),
				"total_tokens":   float64(15),
				"estimated_cost": float64(0.01),
			},
		},
		"by_provider": map[string]any{
			"stub": map[string]any{
				"record_count":   float64(1),
				"input_tokens":   float64(10),
				"output_tokens":  float64(5),
				"total_tokens":   float64(15),
				"estimated_cost": float64(0.01),
			},
		},
	})

	assertContains(t, table, "Usage Summary")
	assertContains(t, table, "records: 2")
	assertContains(t, table, "estimated_cost: $0.03")
	assertContains(t, table, "session_id: sess_1")
	assertContains(t, table, "By Purpose")
	assertContains(t, table, "summarizer")
	assertContains(t, table, "$0.01")
	assertContains(t, table, "By Model")
	assertContains(t, table, "By Provider")
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

func captureRenderEvent(event map[string]any) string {
	oldStdout := os.Stdout
	reader, writer, _ := os.Pipe()
	os.Stdout = writer
	RenderEvent(event)
	writer.Close()
	os.Stdout = oldStdout
	output, _ := io.ReadAll(reader)
	return string(output)
}
