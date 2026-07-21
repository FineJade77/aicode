// Package agentrun implements the common "create session, send message,
// stream events" flow shared by chat/review/diff/test/explain/commit-message.
package agentrun

import (
	"context"
	"fmt"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

func Run(cfg config.Config, mode string, prompt string) error {
	if strings.TrimSpace(prompt) == "" {
		return fmt.Errorf("请输入任务内容")
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	root, err := workspace.Detect()
	if err != nil {
		return err
	}

	api := client.New(cfg.Runtime.URL, daemon.Token())
	session, err := api.CreateSession(ctx, client.CreateSessionRequest{
		Workspace: root.Path,
		Language:  cfg.UI.Language,
	})
	if err != nil {
		return err
	}

	run, err := api.SendMessage(ctx, session.SessionID, client.SendMessageRequest{
		Message:   prompt,
		Mode:      mode,
		Workspace: root.Path,
		Language:  cfg.UI.Language,
	})
	if err != nil {
		return err
	}

	fmt.Printf("会话: %s\n", session.SessionID)
	return runtimeio.StreamAndHandle(ctx, api, session.SessionID, run.RunID)
}
