package renderer

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"text/tabwriter"
)

func PrintJSON(value any) {
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		fmt.Printf("%v\n", value)
		return
	}
	fmt.Println(string(encoded))
}

func PrintReviewRulesTable(value any) {
	fmt.Print(ReviewRulesTable(value))
}

func PrintReviewRulesMarkdown(value any) {
	fmt.Print(ReviewRulesMarkdown(value))
}

func ReviewRulesTable(value any) string {
	root, ok := value.(map[string]any)
	if !ok {
		return fmt.Sprintf("%v\n", value)
	}

	var out strings.Builder
	out.WriteString("Review 配置\n")
	if config, ok := root["effective_config"].(map[string]any); ok {
		out.WriteString(fmt.Sprintf("disabledRules: %s\n", joinStringList(config["disabled_rules"])))
		out.WriteString(fmt.Sprintf("largeDiffThreshold: %v\n", config["large_diff_threshold"]))
		out.WriteString(fmt.Sprintf("maxFindings: %v\n", config["max_findings"]))
	}

	if warnings, ok := root["config_warnings"].([]any); ok && len(warnings) > 0 {
		out.WriteString("\nWarnings\n")
		for _, item := range warnings {
			warning, ok := item.(map[string]any)
			if !ok {
				continue
			}
			out.WriteString(fmt.Sprintf("- %s: %s\n", stringValue(warning["rule"]), stringValue(warning["message"])))
		}
	}

	out.WriteString("\nReview Rules\n")
	var table bytes.Buffer
	writer := tabwriter.NewWriter(&table, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "STATE\tSEVERITY\tRULE\tTITLE")
	if rules, ok := root["rules"].([]any); ok {
		for _, item := range rules {
			rule, ok := item.(map[string]any)
			if !ok {
				continue
			}
			state := "disabled"
			if boolValue(rule["enabled"]) {
				state = "enabled"
			}
			fmt.Fprintf(writer, "%s\t%s\t%s\t%s\n", state, stringValue(rule["severity"]), stringValue(rule["id"]), stringValue(rule["title"]))
		}
	}
	writer.Flush()
	out.WriteString(table.String())
	return out.String()
}

func ReviewRulesMarkdown(value any) string {
	root, ok := value.(map[string]any)
	if !ok {
		return fmt.Sprintf("%v\n", value)
	}

	var out strings.Builder
	out.WriteString("# aicode Review Rules\n\n")
	out.WriteString("## Effective Config\n\n")
	if config, ok := root["effective_config"].(map[string]any); ok {
		out.WriteString(fmt.Sprintf("- disabledRules: `%s`\n", joinStringList(config["disabled_rules"])))
		out.WriteString(fmt.Sprintf("- largeDiffThreshold: `%v`\n", config["large_diff_threshold"]))
		out.WriteString(fmt.Sprintf("- maxFindings: `%v`\n", config["max_findings"]))
	}

	if warnings, ok := root["config_warnings"].([]any); ok && len(warnings) > 0 {
		out.WriteString("\n## Config Warnings\n\n")
		for _, item := range warnings {
			warning, ok := item.(map[string]any)
			if !ok {
				continue
			}
			out.WriteString(fmt.Sprintf("- `%s`: %s\n", stringValue(warning["rule"]), stringValue(warning["message"])))
		}
	}

	out.WriteString("\n## Rules\n\n")
	out.WriteString("| State | Severity | Rule | Description |\n")
	out.WriteString("| --- | --- | --- | --- |\n")
	if rules, ok := root["rules"].([]any); ok {
		for _, item := range rules {
			rule, ok := item.(map[string]any)
			if !ok {
				continue
			}
			state := "disabled"
			if boolValue(rule["enabled"]) {
				state = "enabled"
			}
			out.WriteString(fmt.Sprintf(
				"| %s | %s | `%s` | %s |\n",
				state,
				escapeMarkdownTable(stringValue(rule["severity"])),
				escapeMarkdownTable(stringValue(rule["id"])),
				escapeMarkdownTable(stringValue(rule["description"])),
			))
		}
	}
	return out.String()
}

func RenderEvent(event map[string]any) {
	eventType, _ := event["type"].(string)

	switch eventType {
	case "session.created":
		fmt.Printf("工作区: %s\n", stringValue(event["workspace"]))
	case "plan.created":
		fmt.Println("\n计划:")
		if items, ok := event["items"].([]any); ok {
			for _, item := range items {
				if m, ok := item.(map[string]any); ok {
					fmt.Printf("  - %s\n", stringField(m, "text"))
				}
			}
		}
	case "plan.updated":
		fmt.Printf("计划更新: %s -> %s\n", stringValue(event["item_id"]), stringValue(event["status"]))
	case "tool.started":
		fmt.Printf("工具: %s\n", stringValue(event["tool"]))
	case "tool.output":
		text := strings.TrimSpace(stringValue(event["text"]))
		if text != "" {
			fmt.Println(text)
		}
	case "tool.denied":
		fmt.Printf("工具被策略拦截: %s (%s)\n", stringValue(event["tool"]), stringValue(event["error"]))
	case "tool.error":
		fmt.Printf("工具失败: %s (%s)\n", stringValue(event["tool"]), stringValue(event["error"]))
	case "approval.requested":
		fmt.Printf("需要确认: %s\n", stringValue(event["message"]))
	case "patch.preview":
		fmt.Println(stringValue(event["diff"]))
	case "patch.applied":
		fmt.Println("Patch 已应用。")
	case "patch.rejected":
		fmt.Printf("Patch 已拒绝: %s\n", stringValue(event["reason"]))
	case "usage.recorded":
		fmt.Println(usageLine(event))
	case "final":
		fmt.Printf("\n%s\n", stringValue(event["summary"]))
	default:
		if eventType != "" {
			PrintJSON(event)
		}
	}
}

func usageLine(event map[string]any) string {
	purpose := stringValue(event["purpose"])
	if purpose == "" {
		purpose = "unknown"
	}
	cost := stringValue(event["estimated_cost"])
	if cost == "" {
		cost = "0"
	}
	return fmt.Sprintf(
		"用量: purpose=%s model=%s input=%v output=%v cost=$%s",
		purpose,
		stringValue(event["model"]),
		event["input_tokens"],
		event["output_tokens"],
		cost,
	)
}

func joinStringList(value any) string {
	items, ok := value.([]any)
	if !ok || len(items) == 0 {
		return "[]"
	}
	parts := make([]string, 0, len(items))
	for _, item := range items {
		parts = append(parts, stringValue(item))
	}
	return strings.Join(parts, ", ")
}

func boolValue(value any) bool {
	if v, ok := value.(bool); ok {
		return v
	}
	return false
}

func escapeMarkdownTable(value string) string {
	return strings.ReplaceAll(value, "|", "\\|")
}

func stringField(m map[string]any, key string) string {
	return stringValue(m[key])
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	if s, ok := value.(string); ok {
		return s
	}
	return fmt.Sprint(value)
}
