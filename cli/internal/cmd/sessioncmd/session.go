// Package sessioncmd manages persisted sessions and active runs.
package sessioncmd

import (
	"fmt"

	"github.com/FineJade77/aicode/cli/internal/config"
)

const HelpText = `Usage:
  aicode session list [--limit N] [--offset N]
  aicode session show <session_id|--last>
  aicode session resume <session_id|--last> <message>
  aicode session cancel <session_id|--last>
  aicode session prune [--max-sessions N] [--max-age-days N]
`

func Run(cfg config.Config, args []string) error {
	if len(args) == 0 || isHelp(args[0]) {
		fmt.Print(HelpText)
		return nil
	}

	switch args[0] {
	case "list":
		return runList(cfg, args[1:])
	case "prune":
		return runList(cfg, append([]string{"prune"}, args[1:]...))
	case "show":
		if len(args) != 2 {
			return fmt.Errorf("usage: aicode session show <session_id|--last>")
		}
		return runResume(cfg, args[1:])
	case "resume":
		if len(args) < 3 {
			return fmt.Errorf("usage: aicode session resume <session_id|--last> <message>")
		}
		return runResume(cfg, args[1:])
	case "cancel":
		return runCancel(cfg, args[1:])
	default:
		return fmt.Errorf("unknown session command: %s\n\n%s", args[0], HelpText)
	}
}

func isHelp(value string) bool {
	return value == "help" || value == "--help" || value == "-h"
}
