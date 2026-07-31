// Package runtimecmd manages the local Runtime process and diagnostics.
package runtimecmd

import (
	"fmt"

	"github.com/FineJade77/aicode/cli/internal/config"
)

const HelpText = `Usage:
  aicode runtime start
  aicode runtime stop
  aicode runtime status
  aicode runtime doctor [--json]
  aicode runtime models [--json]
  aicode runtime models probe [--no-tools] [--model <name>] [--json]
  aicode runtime usage [--today|--session <session_id>] [--json]
`

func Run(cfg config.Config, args []string) error {
	if len(args) == 0 || isHelp(args[0]) {
		fmt.Print(HelpText)
		return nil
	}

	switch args[0] {
	case "start", "stop", "status":
		return runDaemon(cfg, args[:1])
	case "doctor":
		return runDoctor(cfg, args[1:])
	case "models":
		return runModels(cfg, args[1:])
	case "usage":
		return runUsage(cfg, args[1:])
	default:
		return fmt.Errorf("unknown runtime command: %s\n\n%s", args[0], HelpText)
	}
}

func isHelp(value string) bool {
	return value == "help" || value == "--help" || value == "-h"
}
