// Package sandboxcmd implements `aicode --sandbox docker <test|build|lint>`.
package sandboxcmd

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

const sandboxTimeout = 30 * time.Minute

func Run(cfg config.Config, sandbox string, args []string) error {
	if sandbox != "docker" {
		return fmt.Errorf("暂只支持: aicode --sandbox docker <test|build|lint>")
	}
	if len(args) != 1 || !supportedSandboxAction(args[0]) {
		return fmt.Errorf("用法: aicode --sandbox docker <test|build|lint>")
	}
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}

	executionID, err := newExecutionID()
	if err != nil {
		return err
	}
	api := client.New(cfg.Runtime.URL, daemon.Token())
	signalContext, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancelTimeout := context.WithTimeout(signalContext, sandboxTimeout+30*time.Second)
	defer cancelTimeout()

	response, err := api.Execute(ctx, client.ExecutionRequest{
		ExecutionID:    executionID,
		Backend:        "docker",
		Action:         args[0],
		Workspace:      root.Path,
		TimeoutSeconds: sandboxTimeout.Seconds(),
	})
	if signalContext.Err() != nil {
		cancelContext, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_, _ = api.CancelExecution(cancelContext, executionID)
		return fmt.Errorf("sandbox execution 已取消")
	}
	if err != nil {
		return err
	}
	renderExecution(response)
	if response.Status != "succeeded" {
		return fmt.Errorf(
			"sandbox execution %s（exit=%d, status=%s）",
			response.ExecutionID,
			response.ExitCode,
			response.Status,
		)
	}
	return nil
}

func supportedSandboxAction(action string) bool {
	switch action {
	case "test", "build", "lint":
		return true
	default:
		return false
	}
}

func renderExecution(response client.ExecutionResponse) {
	fmt.Printf(
		"Sandbox: %s\nAction: %s\nExecution: %s\nStatus: %s\nExit: %d\nDuration: %dms\n",
		response.Backend,
		response.Action,
		response.ExecutionID,
		response.Status,
		response.ExitCode,
		response.DurationMS,
	)
	if output := strings.TrimRight(response.Stdout, "\n"); output != "" {
		fmt.Println(output)
	}
	if output := strings.TrimRight(response.Stderr, "\n"); output != "" {
		fmt.Fprintln(os.Stderr, output)
	}
}

func newExecutionID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("生成 execution id 失败: %w", err)
	}
	return "exec_" + hex.EncodeToString(raw), nil
}
