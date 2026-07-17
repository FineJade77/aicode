package renderer

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
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

func PrintModelRoutes(value any) {
	fmt.Print(ModelRoutesTable(value))
}

func PrintUsageSummary(value any) {
	fmt.Print(UsageSummaryTable(value))
}

func ModelRoutesTable(value any) string {
	root, ok := value.(map[string]any)
	if !ok {
		return fmt.Sprintf("%v\n", value)
	}

	var out strings.Builder
	out.WriteString("Model Routes\n")
	if provider, ok := root["provider"].(map[string]any); ok {
		out.WriteString(fmt.Sprintf("primary: %s (configured: %v)\n", stringValue(provider["primary"]), provider["primary_configured"]))
		out.WriteString(fmt.Sprintf("fallback: %s\n", stringValue(provider["fallback"])))
	}

	out.WriteString("\nRoutes\n")
	writeKeyValueTable(&out, root["routes"], "ROUTE", "MODEL")

	if openai, ok := root["openai_compatible"].(map[string]any); ok {
		out.WriteString("\nOpenAI-compatible\n")
		out.WriteString(fmt.Sprintf("base_url: %s\n", stringValue(openai["base_url"])))
		out.WriteString(fmt.Sprintf("api_key_env: %s\n", stringValue(openai["api_key_env"])))
		out.WriteString(fmt.Sprintf("timeout_seconds: %v\n", openai["timeout_seconds"]))
	}

	if pricing, ok := root["pricing"].(map[string]any); ok {
		out.WriteString(fmt.Sprintf("\nPricing (%s / %s)\n", stringValue(pricing["currency"]), stringValue(pricing["unit"])))
		var table bytes.Buffer
		writer := tabwriter.NewWriter(&table, 0, 0, 2, ' ', 0)
		fmt.Fprintln(writer, "PROVIDER\tMODEL\tINPUT/1M\tOUTPUT/1M")
		if models, ok := pricing["models"].([]any); ok {
			for _, item := range models {
				row, ok := item.(map[string]any)
				if !ok {
					continue
				}
				fmt.Fprintf(
					writer,
					"%s\t%s\t%v\t%v\n",
					stringValue(row["provider"]),
					stringValue(row["model"]),
					row["input_per_1m"],
					row["output_per_1m"],
				)
			}
		}
		writer.Flush()
		out.WriteString(table.String())
	}

	return out.String()
}

func UsageSummaryTable(value any) string {
	root, ok := value.(map[string]any)
	if !ok {
		return fmt.Sprintf("%v\n", value)
	}

	var out strings.Builder
	out.WriteString("Usage Summary\n")
	out.WriteString(fmt.Sprintf("records: %v\n", root["record_count"]))
	out.WriteString(fmt.Sprintf("input_tokens: %v\n", root["total_input_tokens"]))
	out.WriteString(fmt.Sprintf("output_tokens: %v\n", root["total_output_tokens"]))
	out.WriteString(fmt.Sprintf("total_tokens: %v\n", root["total_tokens"]))
	out.WriteString(fmt.Sprintf("estimated_cost: $%s\n", stringValue(root["estimated_cost"])))
	if filters, ok := root["filters"].(map[string]any); ok {
		if sessionID := stringValue(filters["session_id"]); sessionID != "" {
			out.WriteString(fmt.Sprintf("session_id: %s\n", sessionID))
		}
		if date := stringValue(filters["date"]); date != "" {
			out.WriteString(fmt.Sprintf("date: %s\n", date))
		}
	}
	if auditPath := stringValue(root["audit_path"]); auditPath != "" {
		out.WriteString(fmt.Sprintf("audit_path: %s\n", auditPath))
	}

	writeUsageGroup(&out, "By Purpose", root["by_purpose"])
	writeUsageGroup(&out, "By Model", root["by_model"])
	writeUsageGroup(&out, "By Provider", root["by_provider"])
	return out.String()
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

func writeKeyValueTable(out *strings.Builder, value any, keyHeader string, valueHeader string) {
	items, ok := value.(map[string]any)
	if !ok {
		return
	}
	keys := make([]string, 0, len(items))
	for key := range items {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	var table bytes.Buffer
	writer := tabwriter.NewWriter(&table, 0, 0, 2, ' ', 0)
	fmt.Fprintf(writer, "%s\t%s\n", keyHeader, valueHeader)
	for _, key := range keys {
		fmt.Fprintf(writer, "%s\t%s\n", key, stringValue(items[key]))
	}
	writer.Flush()
	out.WriteString(table.String())
}

func writeUsageGroup(out *strings.Builder, title string, value any) {
	group, ok := value.(map[string]any)
	if !ok || len(group) == 0 {
		return
	}
	keys := make([]string, 0, len(group))
	for key := range group {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	out.WriteString("\n" + title + "\n")
	var table bytes.Buffer
	writer := tabwriter.NewWriter(&table, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "KEY\tRECORDS\tINPUT\tOUTPUT\tTOTAL\tCOST")
	for _, key := range keys {
		row, ok := group[key].(map[string]any)
		if !ok {
			continue
		}
		fmt.Fprintf(
			writer,
			"%s\t%v\t%v\t%v\t%v\t$%s\n",
			key,
			row["record_count"],
			row["input_tokens"],
			row["output_tokens"],
			row["total_tokens"],
			stringValue(row["estimated_cost"]),
		)
	}
	writer.Flush()
	out.WriteString(table.String())
}

func RenderEvent(event map[string]any) {
	eventType, _ := event["type"].(string)

	switch eventType {
	case "session.created":
		fmt.Printf("工作区: %s\n", stringValue(event["workspace"]))
	case "run.queued":
		fmt.Println(stringValue(event["message"]))
	case "run.started":
		fmt.Println(stringValue(event["message"]))
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
	case "agent.step":
		action := stringValue(event["action"])
		if action == "tool" {
			fmt.Printf("Agent step %v: %s (%s)\n", event["index"], stringValue(event["tool"]), stringValue(event["source"]))
		} else {
			fmt.Printf("Agent step %v: finish (%s)\n", event["index"], stringValue(event["source"]))
		}
	case "agent.loop.max_steps":
		fmt.Println(stringValue(event["message"]))
	case "tool.started":
		fmt.Printf("工具: %s\n", stringValue(event["tool"]))
	case "tool.output":
		text := strings.TrimSpace(stringValue(event["text"]))
		if text != "" {
			fmt.Println(text)
		}
	case "tool.denied":
		fmt.Printf("工具被策略拦截: %s (%s)\n", stringValue(event["tool"]), stringValue(event["error"]))
	case "tool.rejected":
		fmt.Printf("工具执行已拒绝: %s (%s)\n", stringValue(event["tool"]), stringValue(event["error"]))
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
	case "verification.started":
		fmt.Println(stringValue(event["message"]))
	case "verification.skipped":
		fmt.Printf("验证跳过: %s\n", stringValue(event["reason"]))
	case "verification.completed":
		status := "通过"
		if !boolValue(event["success"]) {
			status = "失败"
		}
		fmt.Printf("验证%s: %s\n", status, stringValue(event["command"]))
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
