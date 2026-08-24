// Package tuicmd launches the full-screen session view.
package tuicmd

import (
	"context"
	"fmt"
	"os"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/tui"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

const HelpText = `Usage:
  aicode tui

Full-screen session view: transcript, plan, context budget and approvals.
Requires a terminal; use ` + "`aicode chat`" + ` when stdin is piped.
`

func Run(cfg config.Config, args []string) error {
	if len(args) > 0 {
		fmt.Print(HelpText)
		return nil
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	terminal, err := tui.OpenTerminal()
	if err != nil {
		return err
	}
	// Restored on every path out, including a panic: leaving raw mode behind
	// gives the user a shell with no echo and no obvious way back.
	defer terminal.Restore()

	session := &tui.Session{
		API:       client.New(cfg.Runtime.URL, daemon.Token()),
		Terminal:  terminal,
		Workspace: root.Path,
		Out:       os.Stdout,
	}
	return session.Run(context.Background())
}
