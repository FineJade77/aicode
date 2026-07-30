package renderer

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"text/tabwriter"
)

func PrintJSON(value any) {
	PrintJSONTo(os.Stdout, value)
}

// PrintJSONTo renders stable, indented JSON to the supplied writer.
func PrintJSONTo(out io.Writer, value any) {
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		fmt.Fprintf(out, "%v\n", value)
		return
	}
	fmt.Fprintln(out, string(encoded))
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

func PrintModelProbe(value any) {
	fmt.Print(ModelProbeTable(value))
}

func PrintUsageSummary(value any) {
	fmt.Print(UsageSummaryTable(value))
}

func PrintDaemonStatus(value any) {
	fmt.Print(DaemonStatusTable(value))
}

func DaemonStatusTable(value any) string {
	root, ok := value.(map[string]any)
	if !ok {
		return fmt.Sprintf("%v\n", value)
	}

	var out strings.Builder
	out.WriteString("Daemon Status\n")
	out.WriteString(fmt.Sprintf("status: %s\n", stringValue(root["status"])))
	out.WriteString(fmt.Sprintf("name: %s\n", stringValue(root["name"])))
	out.WriteString(fmt.Sprintf("version: %s\n", stringValue(root["version"])))
	if pid := stringValue(root["pid"]); pid != "" {
		out.WriteString(fmt.Sprintf("pid: %s\n", pid))
	}

	if writer, ok := root["audit_writer"].(map[string]any); ok {
		out.WriteString("\nAudit Writer\n")
		writeKeyValueTable(&out, writer, "METRIC", "VALUE")
	}

	if writer, ok := root["event_writer"].(map[string]any); ok {
		out.WriteString("\nEvent Writer\n")
		writeKeyValueTable(&out, writer, "METRIC", "VALUE")
	}

	return out.String()
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

	if capabilities, ok := root["capabilities"].(map[string]any); ok {
		out.WriteString("\nContext Capabilities\n")
		var table bytes.Buffer
		writer := tabwriter.NewWriter(&table, 0, 0, 2, ' ', 0)
		fmt.Fprintln(writer, "ROUTE\tPROVIDER\tMODEL\tCONTEXT\tMAX OUTPUT\tTOOLS\tSTREAM\tTOKENIZER\tSOURCE")
		keys := make([]string, 0, len(capabilities))
		for key := range capabilities {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			row, ok := capabilities[key].(map[string]any)
			if !ok {
				continue
			}
			fmt.Fprintf(
				writer,
				"%s\t%s\t%s\t%v\t%v\t%v\t%v\t%s\t%s\n",
				key,
				stringValue(row["provider"]),
				stringValue(row["model"]),
				row["context_window"],
				row["max_output_tokens"],
				row["tool_calling"],
				row["streaming"],
				stringValue(row["tokenizer"]),
				stringValue(row["source"]),
			)
		}
		writer.Flush()
		out.WriteString(table.String())
	}

	if openai, ok := root["openai_compatible"].(map[string]any); ok {
		out.WriteString("\nOpenAI-compatible\n")
		out.WriteString(fmt.Sprintf("profile: %s (schema v%v)\n", stringValue(openai["profile"]), openai["profile_schema_version"]))
		out.WriteString(fmt.Sprintf("base_url: %s\n", stringValue(openai["base_url"])))
		out.WriteString(fmt.Sprintf("auth_mode: %s\n", stringValue(openai["auth_mode"])))
		out.WriteString(fmt.Sprintf("api_key_env: %s\n", stringValue(openai["api_key_env"])))
		out.WriteString(fmt.Sprintf("timeout_seconds: %v\n", openai["timeout_seconds"]))
		out.WriteString(fmt.Sprintf("tool_calling: %v\n", openai["tool_calling"]))
		out.WriteString(fmt.Sprintf("streaming: %v\n", openai["streaming"]))
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

func ModelProbeTable(value any) string {
	root, ok := value.(map[string]any)
	if !ok {
		return fmt.Sprintf("%v\n", value)
	}

	var out strings.Builder
	out.WriteString("Provider Profile Probe\n")
	out.WriteString(fmt.Sprintf("status: %s\n", stringValue(root["status"])))
	out.WriteString(fmt.Sprintf("model: %s\n", stringValue(root["model"])))
	out.WriteString(fmt.Sprintf("latency_ms: %v\n", root["latency_ms"]))
	if profile, ok := root["profile"].(map[string]any); ok {
		out.WriteString("\nProfile\n")
		for _, key := range []string{
			"name", "schema_version", "provider", "base_url", "auth_mode", "context_window",
			"max_output_tokens", "tool_calling", "streaming", "tokenizer", "chars_per_token",
		} {
			out.WriteString(fmt.Sprintf("%s: %v\n", key, profile[key]))
		}
	}

	out.WriteString("\nChecks\n")
	var table bytes.Buffer
	writer := tabwriter.NewWriter(&table, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "STATUS\tCHECK\tCODE\tLATENCY\tSUMMARY")
	if checks, ok := root["checks"].([]any); ok {
		for _, item := range checks {
			check, ok := item.(map[string]any)
			if !ok {
				continue
			}
			fmt.Fprintf(
				writer,
				"%s\t%s\t%s\t%v\t%s\n",
				strings.ToUpper(stringValue(check["status"])),
				stringValue(check["name"]),
				stringValue(check["code"]),
				check["latency_ms"],
				stringValue(check["summary"]),
			)
		}
	}
	writer.Flush()
	out.WriteString(table.String())

	if models := joinStringList(root["discovered_models"]); models != "" {
		out.WriteString("\nDiscovered Models\n")
		out.WriteString(models + "\n")
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
	out.WriteString("Review configuration\n")
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
	RenderEventTo(os.Stdout, event)
}

// RenderEventTo keeps event rendering reusable by the one-shot CLI and REPL.
func RenderEventTo(out io.Writer, event map[string]any) {
	eventType, _ := event["type"].(string)

	switch eventType {
	case "session.created":
		fmt.Fprintf(out, "Workspace: %s\n", stringValue(event["workspace"]))
	case "run.queued":
		fmt.Fprintln(out, stringValue(event["message"]))
	case "run.started":
		fmt.Fprintln(out, stringValue(event["message"]))
	case "run.steer.queued":
		fmt.Fprintln(out, stringValue(event["message"]))
	case "run.steer.applied":
		fmt.Fprintln(out, stringValue(event["message"]))
	case "run.cancelled":
		fmt.Fprintln(out, stringValue(event["message"]))
	case "assistant.delta":
		fmt.Fprint(out, stringValue(event["text"]))
	case "tool.started":
		fmt.Fprintf(out, "Tool: %s\n", stringValue(event["tool"]))
	case "tool.output":
		if detail := toolDetailLine("Tool completed", event); detail != "" {
			fmt.Fprintln(out, detail)
		}
		if line := contextOutputLine(event); line != "" {
			fmt.Fprintln(out, line)
		}
		text := strings.TrimSpace(stringValue(event["text"]))
		if text != "" {
			fmt.Fprintln(out, text)
		}
	case "context.budget":
		if line := contextBudgetLine(event); line != "" {
			fmt.Fprint(out, line)
		}
	case "run.budget.exceeded":
		fmt.Fprint(out, budgetExceededLine(event))
	case "tool.denied":
		fmt.Fprintf(out, "Tool denied by policy: %s (%s)\n", stringValue(event["tool"]), stringValue(event["error"]))
	case "tool.rejected":
		fmt.Fprintf(out, "%s: %s (%s)\n", approvalOutcomeLabel(event, "Tool execution rejected", "Tool execution not approved"), stringValue(event["tool"]), stringValue(event["error"]))
	case "tool.error":
		detail := toolDetailLine("Tool failed", event)
		if detail == "" {
			detail = fmt.Sprintf("Tool failed: %s", stringValue(event["tool"]))
		}
		fmt.Fprintf(out, "%s (%s)\n", detail, stringValue(event["error"]))
	case "approval.requested":
		fmt.Fprintf(out, "Approval required: %s\n", stringValue(event["message"]))
	case "approval.expired":
		fmt.Fprintf(out, "Approval expired: %s\n", stringValue(event["message"]))
	case "edit.applied":
		fmt.Fprintf(out, "\nEdit applied: %v (%v)\n", event["path"], event["kind"])
	case "edit.rejected":
		fmt.Fprintf(out, "\n%s: %v\n", approvalOutcomeLabel(event, "Edit rejected", "Edit not approved (approval timed out)"), event["path"])
	case "edit.auto_approved":
		fmt.Fprintf(out, "\n[allowed for this session] Edit applied automatically: %v\n", event["path"])
	case "usage.recorded":
		fmt.Fprintln(out, usageLine(event))
	case "error":
		fmt.Fprintf(out, "\nError: %s\n", stringValue(event["error"]))
	case "final":
		fmt.Fprintf(out, "\n%s\n", stringValue(event["summary"]))
	default:
		if eventType != "" {
			PrintJSONTo(out, event)
		}
	}
}

func toolDetailLine(label string, event map[string]any) string {
	tool := stringValue(event["tool"])
	if tool == "" {
		return ""
	}
	parts := []string{}
	if event["duration_ms"] != nil {
		parts = append(parts, fmt.Sprintf("%sms", stringValue(event["duration_ms"])))
	}
	if event["exit_code"] != nil {
		parts = append(parts, fmt.Sprintf("exit=%s", stringValue(event["exit_code"])))
	}
	if boolValue(event["timed_out"]) {
		parts = append(parts, "timeout")
	}
	if len(parts) == 0 {
		return ""
	}
	return fmt.Sprintf("%s: %s [%s]", label, tool, strings.Join(parts, ", "))
}

func contextOutputLine(event map[string]any) string {
	tool := stringValue(event["tool"])
	data := mapValue(event["data"])
	context := mapValue(event["context"])
	kind := stringValue(context["kind"])
	sourcePath := stringValue(context["source_path"])

	switch tool {
	case "read_file":
		path := stringValue(data["path"])
		if path == "" {
			return ""
		}
		line := fmt.Sprintf("Context: read %s", path)
		if label := contextKindLabel(kind); label != "" && sourcePath != "" {
			line += fmt.Sprintf(" (%s: %s)", label, sourcePath)
		}
		if boolValue(data["truncated"]) {
			line += "; tool output was truncated"
		}
		return line
	case "find_files":
		if kind == "" {
			return ""
		}
		label := contextKindLabel(kind)
		if label == "" {
			return ""
		}
		query := stringValue(data["query"])
		count := len(sliceValue(data["files"]))
		if sourcePath != "" && query != "" {
			return fmt.Sprintf("Context: %s %s -> %s, %d candidates", label, sourcePath, query, count)
		}
		if query != "" {
			return fmt.Sprintf("Context: %s query=%s, %d candidates", label, query, count)
		}
	}
	return ""
}

// budgetExceededLine reports a stopped run prominently: the user is about to get
// a shorter answer than they asked for, and needs to know it was a spend cap
// rather than the model deciding the task was finished.
// approvalOutcomeLabel keeps an unanswered approval from being reported as a
// refusal. The user needs to know whether they declined or simply missed the
// prompt, because only the second case is worth retrying.
func approvalOutcomeLabel(event map[string]any, rejected string, timedOut string) string {
	if stringValue(event["resolution"]) == "timed_out" {
		return timedOut
	}
	return rejected
}

func budgetExceededLine(event map[string]any) string {
	reason := stringValue(event["reason"])
	if reason == "" {
		reason = "budget"
	}
	return fmt.Sprintf(
		"\nTurn stopped: %s budget exhausted (tokens=%v, cost=%v, limit=%v). Wrapping up without further tool calls.\n",
		reason,
		event["total_tokens"],
		event["total_cost"],
		event["limit"],
	)
}

func contextBudgetLine(event map[string]any) string {
	if !boolValue(event["compacted"]) {
		return ""
	}
	if event["before_tokens"] != nil || event["after_tokens"] != nil {
		purpose := stringValue(event["purpose"])
		if purpose == "" {
			purpose = "model"
		}
		return fmt.Sprintf(
			"Context budget: %s %v -> %v tokens\n",
			purpose,
			event["before_tokens"],
			event["after_tokens"],
		)
	}
	var out strings.Builder
	purpose := stringValue(event["purpose"])
	if purpose == "" {
		purpose = "model"
	}
	out.WriteString(fmt.Sprintf(
		"Context budget: %s compacted %v observations, now approximately %v/%v chars\n",
		purpose,
		event["per_observation_compactions"],
		event["estimated_observation_chars"],
		event["total_budget_chars"],
	))
	for _, item := range sliceValue(event["compacted_observations"]) {
		row := mapValue(item)
		tool := stringValue(row["tool"])
		target := stringValue(row["path"])
		if target == "" {
			target = stringValue(row["query"])
		}
		if target == "" {
			target = "unknown"
		}
		before := row["text_original_chars"]
		after := row["text_kept_chars"]
		if before == nil {
			before = row["data_original_chars"]
			after = row["data_kept_chars"]
		}
		out.WriteString(fmt.Sprintf("  - %s %s: %v -> %v chars\n", tool, target, before, after))
	}
	return out.String()
}

func contextKindLabel(kind string) string {
	switch kind {
	case "test_mapping":
		return "test mapping"
	case "dependency_mapping":
		return "dependency mapping"
	case "search_result":
		return "search matches"
	case "file_lookup":
		return "file lookup"
	default:
		return ""
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
		"Usage: purpose=%s model=%s input=%v output=%v cost=$%s",
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

func mapValue(value any) map[string]any {
	if v, ok := value.(map[string]any); ok {
		return v
	}
	return map[string]any{}
}

func sliceValue(value any) []any {
	if v, ok := value.([]any); ok {
		return v
	}
	return []any{}
}

func escapeMarkdownTable(value string) string {
	return strings.ReplaceAll(value, "|", "\\|")
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
