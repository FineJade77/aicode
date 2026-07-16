package renderer

import (
	"encoding/json"
	"fmt"
	"strings"
)

func PrintJSON(value any) {
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		fmt.Printf("%v\n", value)
		return
	}
	fmt.Println(string(encoded))
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
	case "approval.requested":
		fmt.Printf("需要确认: %s\n", stringValue(event["message"]))
	case "patch.preview":
		fmt.Println(stringValue(event["diff"]))
	case "patch.applied":
		fmt.Println("Patch 已应用。")
	case "usage.recorded":
		fmt.Printf("用量: model=%s input=%v output=%v\n", stringValue(event["model"]), event["input_tokens"], event["output_tokens"])
	case "final":
		fmt.Printf("\n%s\n", stringValue(event["summary"]))
	default:
		if eventType != "" {
			PrintJSON(event)
		}
	}
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
