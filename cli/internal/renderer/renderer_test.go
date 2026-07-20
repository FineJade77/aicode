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

func TestRenderEventPrintsReadFileContextStatus(t *testing.T) {
	output := captureRenderEvent(map[string]any{
		"type": "tool.output",
		"tool": "read_file",
		"text": "# src/utils.py\ncontent",
		"data": map[string]any{
			"path":      "src/utils.py",
			"truncated": true,
		},
		"context": map[string]any{
			"kind":        "dependency_mapping",
			"source_path": "src/service.py",
		},
	})

	assertContains(t, output, "上下文: 已读取 src/utils.py (依赖映射: src/service.py)，工具输出已截断")
	assertContains(t, output, "# src/utils.py")
}

func TestRenderEventPrintsToolObservability(t *testing.T) {
	output := captureRenderEvent(map[string]any{
		"type":        "tool.output",
		"tool":        "bash",
		"text":        "exit=0\nok",
		"duration_ms": float64(12),
		"exit_code":   float64(0),
	})

	assertContains(t, output, "工具完成: bash [12ms, exit=0]")
	assertContains(t, output, "exit=0")
}

func TestRenderEventPrintsToolErrorObservability(t *testing.T) {
	output := captureRenderEvent(map[string]any{
		"type":        "tool.error",
		"tool":        "bash",
		"error":       "命令超时",
		"duration_ms": float64(1000),
		"exit_code":   float64(-9),
		"timed_out":   true,
	})

	assertContains(t, output, "工具失败: bash [1000ms, exit=-9, timeout] (命令超时)")
}

func TestRenderAssistantDelta(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "assistant.delta", "text": "你好"})
	assertContains(t, output, "你好")
	if strings.HasSuffix(output, "\n\n") {
		t.Fatalf("delta 不应额外换行: %q", output)
	}
}

func TestRenderEditApplied(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "edit.applied", "path": "a.py", "kind": "replace"})
	assertContains(t, output, "a.py")
}

func TestRenderEditRejected(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "edit.rejected", "path": "a.py"})
	assertContains(t, output, "a.py")
	assertContains(t, output, "已拒绝编辑")
}

func TestRenderEditAutoApproved(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "edit.auto_approved", "path": "a.py"})
	assertContains(t, output, "a.py")
	assertContains(t, output, "自动应用编辑")
}

func TestRenderEventPrintsContextBudget(t *testing.T) {
	output := captureRenderEvent(map[string]any{
		"type":                        "context.budget",
		"purpose":                     "coder",
		"compacted":                   true,
		"per_observation_compactions": float64(1),
		"estimated_observation_chars": float64(1000),
		"total_budget_chars":          float64(52000),
		"compacted_observations": []any{
			map[string]any{
				"tool":                "read_file",
				"path":                "src/big.py",
				"text_original_chars": float64(30000),
				"text_kept_chars":     float64(18000),
			},
		},
	})

	assertContains(t, output, "上下文预算: coder 压缩 1 条观测")
	assertContains(t, output, "read_file src/big.py: 30000 -> 18000 chars")
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

func TestDaemonStatusTable(t *testing.T) {
	table := DaemonStatusTable(map[string]any{
		"status":  "ok",
		"name":    "aicode-runtime",
		"version": "0.1.0",
		"pid":     float64(1234),
		"audit_writer": map[string]any{
			"queue_size":     float64(0),
			"queue_max_size": float64(5000),
			"writer_running": true,
			"enqueued":       float64(7),
			"written":        float64(7),
			"dropped":        float64(0),
			"failed":         float64(0),
		},
		"event_writer": map[string]any{
			"queue_size":     float64(0),
			"queue_max_size": float64(5000),
			"writer_running": true,
			"enqueued":       float64(10),
			"written":        float64(9),
			"dropped":        float64(1),
			"failed":         float64(0),
		},
	})

	assertContains(t, table, "Daemon Status")
	assertContains(t, table, "status: ok")
	assertContains(t, table, "Audit Writer")
	assertContains(t, table, "Event Writer")
	assertContains(t, table, "queue_size")
	assertContains(t, table, "dropped")
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
