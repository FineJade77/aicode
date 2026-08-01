package sessioncmd

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
)

// runFork branches a session so a different approach can be tried from a point
// in the conversation without replaying the whole thing.
func runFork(cfg config.Config, args []string) error {
	sessionArgs, messageID, err := parseForkArgs(args)
	if err != nil {
		return err
	}
	sessionID, err := resolveSessionID(cfg, sessionArgs)
	if err != nil {
		return forkUsage()
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), runtimeio.DefaultTimeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL, daemon.Token())
	response, err := api.ForkSession(ctx, sessionID, messageID)
	if err != nil {
		return err
	}
	fmt.Printf(
		"Forked %s into %s with %d message(s).\nContinue it with: aicode session resume %s <message>\n",
		response.SourceSessionID,
		response.SessionID,
		response.MessageCount,
		response.SessionID,
	)
	return nil
}

func parseForkArgs(args []string) ([]string, *int, error) {
	rest := make([]string, 0, len(args))
	var messageID *int
	for index := 0; index < len(args); index++ {
		arg := args[index]
		switch {
		case arg == "--message":
			if index+1 >= len(args) {
				return nil, nil, fmt.Errorf("--message needs a message id")
			}
			index++
			value, err := strconv.Atoi(strings.TrimSpace(args[index]))
			if err != nil {
				return nil, nil, fmt.Errorf("--message must be a message id: %s", args[index])
			}
			messageID = &value
		case strings.HasPrefix(arg, "--message="):
			value, err := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(arg, "--message=")))
			if err != nil {
				return nil, nil, fmt.Errorf("--message must be a message id: %s", arg)
			}
			messageID = &value
		default:
			rest = append(rest, arg)
		}
	}
	return rest, messageID, nil
}

func forkUsage() error {
	return fmt.Errorf("usage: aicode session fork <session_id|--last> [--message N]")
}
