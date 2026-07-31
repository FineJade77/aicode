// Package projectcmd manages project trust, policy, configuration, and sandbox execution.
package projectcmd

import (
	"fmt"

	"github.com/FineJade77/aicode/cli/internal/config"
)

const HelpText = `Usage:
  aicode project trust [status|add|remove|list] [--json]
  aicode project review <list|docs|enable|disable|set|unset|prune> ...
  aicode project protected <add|remove|list|reset> ...
  aicode project command test <set|auto|show|unset> ...
  aicode project workspace <add|remove|list> ...
  aicode project sandbox <test|build|lint>
`

func Run(cfg config.Config, args []string) error {
	if len(args) == 0 || isHelp(args[0]) {
		fmt.Print(HelpText)
		return nil
	}

	switch args[0] {
	case "trust":
		return runTrust(cfg, args[1:])
	case "review":
		return RunReview(cfg, args[1:])
	case "protected":
		return RunProtected(args[1:])
	case "workspace":
		return RunWorkspace(args[1:])
	case "command":
		if len(args) < 2 || args[1] != "test" {
			return fmt.Errorf("usage: aicode project command test <set|auto|show|unset> ...")
		}
		return RunTestCommand(args[2:])
	case "sandbox":
		return runSandbox(cfg, "docker", args[1:])
	case "review-rules-json":
		return PrintReviewRulesJSON(cfg)
	default:
		return fmt.Errorf("unknown project command: %s\n\n%s", args[0], HelpText)
	}
}

func RunSandbox(cfg config.Config, sandbox string, args []string) error {
	return runSandbox(cfg, sandbox, args)
}

func RunReview(cfg config.Config, args []string) error {
	return runReview(cfg, args)
}

func RunProtected(args []string) error {
	return runProtected(args)
}

func RunTestCommand(args []string) error {
	return runTestCommand(args)
}

func RunWorkspace(args []string) error {
	return runWorkspace(args)
}

func isHelp(value string) bool {
	return value == "help" || value == "--help" || value == "-h"
}
