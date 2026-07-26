// Package cancelcmd implements `aicode cancel <session_id|--last>`.
package cancelcmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
)

func Run(cfg config.Config, args []string) error {
	sessionID, err := resolveSessionID(cfg, args)
	if err != nil {
		return err
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), runtimeio.DefaultTimeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL, daemon.Token())
	response, err := api.CancelRun(ctx, sessionID)
	if err != nil {
		return err
	}
	switch response.Status {
	case "cancelled":
		runID := ""
		if response.RunID != nil {
			runID = *response.RunID
		}
		fmt.Printf("已取消任务 %s；队列中剩余 %d 个任务。\n", runID, response.Queued)
	case "idle":
		fmt.Println("该会话当前没有正在执行的任务。")
	default:
		fmt.Printf("取消请求状态: %s\n", response.Status)
	}
	return nil
}

func resolveSessionID(cfg config.Config, args []string) (string, error) {
	if len(args) != 1 {
		return "", usage()
	}
	target := strings.TrimSpace(args[0])
	if target != "--last" {
		if target == "" {
			return "", usage()
		}
		return target, nil
	}

	value, err := runtimeio.FetchJSON(cfg, "/v1/sessions?last=true")
	if err != nil {
		return "", err
	}
	payload, ok := value.(map[string]any)
	if !ok || payload == nil {
		return "", fmt.Errorf("没有可取消的 session")
	}
	sessionID, _ := payload["session_id"].(string)
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return "", fmt.Errorf("session 响应缺少 session_id")
	}
	return sessionID, nil
}

func usage() error {
	return fmt.Errorf("用法: aicode cancel <session_id> 或 aicode cancel --last")
}
