package renderer

import (
	"bytes"
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

	want := "Usage: purpose=reviewer model=stub input=12 output=3 cost=$0.00012345"
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

	want := "Usage: purpose=unknown model=stub input=12 output=3 cost=$0"
	if line != want {
		t.Fatalf("usageLine() = %q, want %q", line, want)
	}
}

func TestRenderEventPrintsRunStatus(t *testing.T) {
	output := captureRenderEvent(map[string]any{
		"type":    "run.queued",
		"message": "The run is queued until the previous run finishes.",
	})

	assertContains(t, output, "run is queued")
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

	assertContains(t, output, "Context: read src/utils.py (dependency mapping: src/service.py); tool output was truncated")
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

	assertContains(t, output, "Tool completed: bash [12ms, exit=0]")
	assertContains(t, output, "exit=0")
}

func TestRenderEventPrintsToolErrorObservability(t *testing.T) {
	output := captureRenderEvent(map[string]any{
		"type":        "tool.error",
		"tool":        "bash",
		"error":       "command timed out",
		"duration_ms": float64(1000),
		"exit_code":   float64(-9),
		"timed_out":   true,
	})

	assertContains(t, output, "Tool failed: bash [1000ms, exit=-9, timeout] (command timed out)")
}

func TestRenderAssistantDelta(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "assistant.delta", "text": "hello"})
	assertContains(t, output, "hello")
	if strings.HasSuffix(output, "\n\n") {
		t.Fatalf("delta should not add an extra newline: %q", output)
	}
}

func TestRenderEditApplied(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "edit.applied", "path": "a.py", "kind": "replace"})
	assertContains(t, output, "a.py")
}

func TestRenderEditRejected(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "edit.rejected", "path": "a.py"})
	assertContains(t, output, "a.py")
	assertContains(t, output, "Edit rejected")
}

func TestRenderEditAutoApproved(t *testing.T) {
	output := captureRenderEvent(map[string]any{"type": "edit.auto_approved", "path": "a.py"})
	assertContains(t, output, "a.py")
	assertContains(t, output, "Edit applied automatically")
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

	assertContains(t, output, "Context budget: coder compacted 1 observations")
	assertContains(t, output, "read_file src/big.py: 30000 -> 18000 chars")
}

func TestReviewRulesTable(t *testing.T) {
	table := ReviewRulesTable(reviewRulesFixture())

	assertContains(t, table, "Review configuration")
	assertContains(t, table, "disabledRules: large_diff, old_rule")
	assertContains(t, table, "largeDiffThreshold: 1200")
	assertContains(t, table, "Warnings")
	assertContains(t, table, "old_rule: disabledRules contains unknown rule old_rule; this entry has no effect.")
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
	assertContains(t, doc, "| disabled | medium | `large_diff` | The diff exceeds the configured threshold |")
	assertContains(t, doc, "| enabled | high | `secret_added` | Added content matches a credential pattern |")
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
		"capabilities": map[string]any{
			"main": map[string]any{
				"provider":          "openai_compatible",
				"model":             "local-8k",
				"context_window":    float64(8192),
				"max_output_tokens": float64(2048),
				"source":            "configured",
			},
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
	assertContains(t, table, "Context Capabilities")
	assertContains(t, table, "local-8k")
	assertContains(t, table, "8192")
	assertContains(t, table, "OpenAI-compatible")
	assertContains(t, table, "base_url: https://api.example.com/v1")
	assertContains(t, table, "Pricing (USD / per_1m_tokens)")
	assertContains(t, table, "openai_compatible")
}

func TestModelProbeTable(t *testing.T) {
	table := ModelProbeTable(map[string]any{
		"status":     "ok",
		"model":      "local-coder",
		"latency_ms": float64(12),
		"profile": map[string]any{
			"name":              "ollama",
			"schema_version":    float64(1),
			"provider":          "openai_compatible",
			"base_url":          "http://127.0.0.1:11434/v1",
			"auth_mode":         "none",
			"context_window":    float64(32768),
			"max_output_tokens": float64(4096),
			"tool_calling":      true,
			"streaming":         true,
			"tokenizer":         "chars",
			"chars_per_token":   float64(3.5),
		},
		"checks": []any{
			map[string]any{
				"name":       "endpoint",
				"status":     "pass",
				"code":       "reachable",
				"summary":    "discovered 1 model",
				"latency_ms": float64(4),
			},
			map[string]any{
				"name":    "tools",
				"status":  "pass",
				"code":    "tools_ok",
				"summary": "native tool calling works",
			},
		},
		"discovered_models": []any{"local-coder"},
	})

	for _, expected := range []string{
		"Provider Profile Probe",
		"status: ok",
		"name: ollama",
		"auth_mode: none",
		"PASS",
		"tools_ok",
		"local-coder",
	} {
		assertContains(t, table, expected)
	}
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
				"message": "disabledRules contains unknown rule old_rule; this entry has no effect.",
			},
		},
		"rules": []any{
			map[string]any{
				"id":          "large_diff",
				"severity":    "medium",
				"enabled":     false,
				"title":       "Large diff",
				"description": "The diff exceeds the configured threshold",
			},
			map[string]any{
				"id":          "secret_added",
				"severity":    "high",
				"enabled":     true,
				"title":       "Added line may contain a secret",
				"description": "Added content matches a credential pattern",
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

func TestRenderTimedOutApprovalIsNotShownAsRejection(t *testing.T) {
	// The user needs to know whether they declined or simply missed the prompt,
	// because only the second case is worth retrying.
	var out bytes.Buffer
	RenderEventTo(&out, map[string]any{
		"type":       "edit.rejected",
		"path":       "calc.py",
		"resolution": "timed_out",
	})
	text := out.String()
	if !strings.Contains(text, "timed out") {
		t.Fatalf("expected a timeout to be reported as such, got %q", text)
	}
	if strings.Contains(text, "Edit rejected") {
		t.Fatalf("a timeout must not read as a rejection, got %q", text)
	}
}

func TestRenderExplicitEditRejectionStillReadsAsRejection(t *testing.T) {
	var out bytes.Buffer
	RenderEventTo(&out, map[string]any{
		"type":       "edit.rejected",
		"path":       "calc.py",
		"resolution": "rejected",
	})
	if text := out.String(); !strings.Contains(text, "Edit rejected") {
		t.Fatalf("expected an explicit rejection label, got %q", text)
	}
}

func TestRenderPlanAsChecklist(t *testing.T) {
	// The user-visible payoff of externalising the plan: a checklist instead of
	// a stream of tool names.
	var out bytes.Buffer
	RenderEventTo(&out, map[string]any{
		"type": "plan.updated",
		"items": []any{
			map[string]any{"text": "Locate the bug", "status": "done"},
			map[string]any{"text": "Fix it", "status": "in_progress"},
			map[string]any{"text": "Run tests", "status": "pending"},
		},
	})
	text := out.String()
	for _, want := range []string{"[x] Locate the bug", "[~] Fix it", "[ ] Run tests"} {
		if !strings.Contains(text, want) {
			t.Fatalf("expected %q in output, got %q", want, text)
		}
	}
}

func TestRenderEmptyPlanSaysCleared(t *testing.T) {
	var out bytes.Buffer
	RenderEventTo(&out, map[string]any{"type": "plan.updated", "items": []any{}})
	if text := out.String(); !strings.Contains(text, "Plan cleared") {
		t.Fatalf("expected a cleared notice, got %q", text)
	}
}
