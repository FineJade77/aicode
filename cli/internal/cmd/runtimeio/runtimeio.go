package runtimeio

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/renderer"
)

const DefaultTimeout = 10 * time.Second

func EnsureDaemon(cfg config.Config) error {
	ctx, cancel := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel()
	if _, err := daemon.Status(ctx, cfg.Runtime.URL); err == nil {
		return nil
	}

	fmt.Println("Runtime daemon 未运行，正在启动...")
	if err := daemon.Start(cfg); err != nil {
		return err
	}
	return daemon.WaitUntilReady(cfg.Runtime.URL, 5*time.Second)
}

func FetchJSON(cfg config.Config, path string) (any, error) {
	if err := EnsureDaemon(cfg); err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(context.Background(), DefaultTimeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL, daemon.Token())
	return api.GetJSON(ctx, path)
}

func RunSimpleGet(cfg config.Config, path string) error {
	value, err := FetchJSON(cfg, path)
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
}

// StreamAndHandle streams run events to the terminal and resolves any
// interactive approval prompts raised along the way.
func StreamAndHandle(ctx context.Context, api client.Client, sessionID string, runID string) error {
	return api.StreamRunEvents(ctx, sessionID, runID, func(event map[string]any) error {
		renderer.RenderEvent(event)
		return handleInteractiveEvent(api, sessionID, event)
	})
}

func handleInteractiveEvent(api client.Client, sessionID string, event map[string]any) error {
	eventType, _ := event["type"].(string)
	switch eventType {
	case "approval.requested":
		approvalID, _ := event["approval_id"].(string)
		if approvalID == "" {
			return fmt.Errorf("approval.requested 缺少 approval_id")
		}
		if kind, _ := event["kind"].(string); kind == "edit" {
			if diff, _ := event["diff"].(string); diff != "" {
				fmt.Println(diff)
			}
			return resolveEditApproval(api, sessionID, approvalID)
		}
		return resolveApprovalWithPrompt(api, sessionID, approvalID, "允许执行这个工具操作吗？输入 y 确认，其它任意输入拒绝 [y/N]: ")
	default:
		return nil
	}
}

func resolveApprovalWithPrompt(api client.Client, sessionID string, approvalID string, prompt string) error {
	fmt.Print(prompt)
	reader := bufio.NewReader(os.Stdin)
	answer, err := reader.ReadString('\n')
	if err != nil && len(answer) == 0 {
		answer = "n"
	}

	ctx, cancel := context.WithTimeout(context.Background(), DefaultTimeout)
	defer cancel()

	if strings.EqualFold(strings.TrimSpace(answer), "y") {
		return api.Approve(ctx, sessionID, approvalID, false)
	}
	return api.Reject(ctx, sessionID, approvalID)
}

func resolveEditApproval(api client.Client, sessionID string, approvalID string) error {
	fmt.Print("应用这个编辑吗？[y=应用 / a=应用并允许本会话后续编辑 / 其它=拒绝]: ")
	reader := bufio.NewReader(os.Stdin)
	line, _ := reader.ReadString('\n')
	answer := strings.ToLower(strings.TrimSpace(line))
	ctx, cancel := context.WithTimeout(context.Background(), DefaultTimeout)
	defer cancel()
	switch answer {
	case "y", "yes":
		return api.Approve(ctx, sessionID, approvalID, false)
	case "a", "all":
		return api.Approve(ctx, sessionID, approvalID, true)
	default:
		return api.Reject(ctx, sessionID, approvalID)
	}
}
