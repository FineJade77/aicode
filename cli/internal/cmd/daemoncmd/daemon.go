// Package daemoncmd implements `aicode daemon <start|stop|status>`.
package daemoncmd

import (
	"context"
	"fmt"

	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/renderer"
)

func Run(cfg config.Config, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: aicode daemon <start|stop|status>")
	}

	switch args[0] {
	case "start":
		if err := daemon.Start(cfg); err != nil {
			return err
		}
		fmt.Println("Runtime daemon started.")
		return nil
	case "stop":
		if err := daemon.Stop(); err != nil {
			return err
		}
		fmt.Println("Runtime daemon stopped.")
		return nil
	case "status":
		ctx, cancel := context.WithTimeout(context.Background(), runtimeio.DefaultTimeout)
		defer cancel()
		status, err := daemon.Status(ctx, cfg.Runtime.URL)
		if err != nil {
			return err
		}
		renderer.PrintDaemonStatus(status)
		return nil
	default:
		return fmt.Errorf("unknown daemon command: %s", args[0])
	}
}
