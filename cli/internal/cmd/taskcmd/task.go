package taskcmd

import (
	"fmt"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/config"
)

const HelpText = `Usage:
  aicode task review
  aicode task diff
  aicode task test
  aicode task explain <file-or-symbol>
  aicode task commit-message
`

func Run(cfg config.Config, args []string) error {
	if len(args) == 0 || isHelp(args[0]) {
		fmt.Print(HelpText)
		return nil
	}

	switch args[0] {
	case "review":
		if len(args) != 1 {
			return taskUsage()
		}
		return RunPrompt(cfg, "review", "Review the current code changes.")
	case "diff":
		if len(args) != 1 {
			return taskUsage()
		}
		return RunPrompt(cfg, "diff", "Inspect the current git diff and summarize the changes.")
	case "test":
		if len(args) != 1 {
			return taskUsage()
		}
		return RunPrompt(cfg, "test", "Discover and run this project's low-risk test command.")
	case "explain":
		if len(args) < 2 {
			return fmt.Errorf("usage: aicode task explain <file-or-symbol>")
		}
		return RunPrompt(cfg, "explain", "Explain "+strings.Join(args[1:], " "))
	case "commit-message":
		if len(args) != 1 {
			return taskUsage()
		}
		return runCommitMessage(cfg)
	default:
		return fmt.Errorf("unknown task command: %s\n\n%s", args[0], HelpText)
	}
}

func taskUsage() error {
	return fmt.Errorf("%s", strings.TrimSpace(HelpText))
}

func isHelp(value string) bool {
	return value == "help" || value == "--help" || value == "-h"
}
