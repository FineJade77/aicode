package sessioncmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
)

func runCancel(cfg config.Config, args []string) error {
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
		fmt.Printf("Cancelled run %s; %d runs remain queued.\n", runID, response.Queued)
	case "idle":
		fmt.Println("This session has no active run.")
	default:
		fmt.Printf("Cancellation status: %s\n", response.Status)
	}
	return nil
}

func resolveSessionID(cfg config.Config, args []string) (string, error) {
	if len(args) != 1 {
		return "", cancelUsage()
	}
	target := strings.TrimSpace(args[0])
	if target != "--last" {
		if target == "" {
			return "", cancelUsage()
		}
		return target, nil
	}

	value, err := runtimeio.FetchJSON(cfg, "/v1/sessions?last=true")
	if err != nil {
		return "", err
	}
	payload, ok := value.(map[string]any)
	if !ok || payload == nil {
		return "", fmt.Errorf("no session is available to cancel")
	}
	sessionID, _ := payload["session_id"].(string)
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return "", fmt.Errorf("session response is missing session_id")
	}
	return sessionID, nil
}

func cancelUsage() error {
	return fmt.Errorf("usage: aicode session cancel <session_id|--last>")
}
