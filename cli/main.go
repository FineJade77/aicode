package main

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/cmd/chatcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/configcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/projectcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimecmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/sessioncmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/taskcmd"
	"github.com/FineJade77/aicode/cli/internal/config"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		printHelp()
		return nil
	}
	if args[0] == "help" || args[0] == "--help" || args[0] == "-h" {
		return printHelpFor(args[1:])
	}
	if category, ok := categoryHelpRequest(args); ok {
		return printHelpFor([]string{category})
	}

	cfg, err := config.Load()
	if err != nil {
		return err
	}
	warnAboutDeprecatedConfig(os.Stderr, cfg)

	options, commandArgs, err := parseGlobalArgs(args)
	if err != nil {
		return err
	}
	if options.Sandbox != "" {
		return projectcmd.RunSandbox(cfg, options.Sandbox, commandArgs)
	}
	args = normalizeLegacyArgs(commandArgs)

	if len(args) == 0 {
		printHelp()
		return nil
	}

	switch args[0] {
	case "chat":
		if len(args) == 1 {
			return chatcmd.Run(cfg)
		}
		if isHelp(args[1]) {
			fmt.Print(chatHelpText)
			return nil
		}
		return taskcmd.RunPrompt(cfg, "chat", strings.Join(args[1:], " "))
	case "task":
		return taskcmd.Run(cfg, args[1:])
	case "session":
		return sessioncmd.Run(cfg, args[1:])
	case "runtime":
		return runtimecmd.Run(cfg, args[1:])
	case "project":
		return projectcmd.Run(cfg, args[1:])
	case "config":
		return configcmd.Run(cfg, args[1:])
	default:
		return taskcmd.RunPrompt(cfg, "default", strings.Join(args, " "))
	}
}

// normalizeLegacyArgs keeps existing scripts working without exposing the old
// flat command list in help or duplicating dispatch logic.
func normalizeLegacyArgs(args []string) []string {
	if len(args) == 0 {
		return args
	}
	tail := args[1:]
	switch args[0] {
	case "sessions":
		if len(tail) > 0 && tail[0] == "prune" {
			return append([]string{"session", "prune"}, tail[1:]...)
		}
		return append([]string{"session", "list"}, tail...)
	case "resume":
		if len(tail) == 1 {
			return append([]string{"session", "show"}, tail...)
		}
		return append([]string{"session", "resume"}, tail...)
	case "cancel":
		return append([]string{"session", "cancel"}, tail...)
	case "daemon":
		return append([]string{"runtime"}, tail...)
	case "doctor":
		return append([]string{"runtime", "doctor"}, tail...)
	case "models":
		return append([]string{"runtime", "models"}, tail...)
	case "usage":
		return append([]string{"runtime", "usage"}, tail...)
	case "trust":
		return append([]string{"project", "trust"}, tail...)
	case "review-rules":
		return []string{"project", "review-rules-json"}
	case "repl":
		return []string{"chat"}
	case "review", "diff", "test", "commit-message", "explain":
		return append([]string{"task", args[0]}, tail...)
	default:
		return args
	}
}

func printHelp() {
	fmt.Print(rootHelpText)
}

const rootHelpText = `aicode - Local-first CLI coding agent

Usage:
  aicode "<task>"
  aicode chat [message]
  aicode <category> <command>

Categories:
  task       One-shot review, diff, test, explain, and commit-message tasks
  session    List, inspect, resume, cancel, and prune sessions
  runtime    Manage the daemon, diagnostics, models, and usage
  project    Manage trust, project policy, workspaces, and sandbox runs
  config     Manage global CLI and Runtime configuration

Run ` + "`aicode help <category>`" + ` for category-specific commands.
`

const chatHelpText = `Usage:
  aicode chat
  aicode chat <message>
`

func printHelpFor(args []string) error {
	if len(args) == 0 {
		printHelp()
		return nil
	}
	if len(args) != 1 {
		return fmt.Errorf("usage: aicode help [task|session|runtime|project|config|chat]")
	}
	switch args[0] {
	case "task":
		fmt.Print(taskcmd.HelpText)
	case "session":
		fmt.Print(sessioncmd.HelpText)
	case "runtime":
		fmt.Print(runtimecmd.HelpText)
	case "project":
		fmt.Print(projectcmd.HelpText)
	case "config":
		fmt.Print(configcmd.HelpText)
	case "chat":
		fmt.Print(chatHelpText)
	default:
		return fmt.Errorf("unknown help category: %s", args[0])
	}
	return nil
}

func isHelp(value string) bool {
	return value == "help" || value == "--help" || value == "-h"
}

func categoryHelpRequest(args []string) (string, bool) {
	if len(args) == 0 || !isCategory(args[0]) {
		return "", false
	}
	if len(args) == 1 && args[0] != "chat" {
		return args[0], true
	}
	if len(args) == 2 && isHelp(args[1]) {
		return args[0], true
	}
	return "", false
}

func isCategory(value string) bool {
	switch value {
	case "chat", "task", "session", "runtime", "project", "config":
		return true
	default:
		return false
	}
}

type globalOptions struct {
	Sandbox string
}

func parseGlobalArgs(args []string) (globalOptions, []string, error) {
	options := globalOptions{}
	for len(args) > 0 {
		switch args[0] {
		case "--sandbox":
			if len(args) < 2 || strings.TrimSpace(args[1]) == "" {
				return options, nil, fmt.Errorf("usage: aicode project sandbox <test|build|lint>")
			}
			options.Sandbox = args[1]
			args = args[2:]
		default:
			return options, args, nil
		}
	}
	return options, args, nil
}

// warnAboutDeprecatedConfig tells the user once per invocation that a legacy
// setting was read, and what it did.
//
// On stderr so that `--json` output stays machine-parseable on stdout: a
// migration notice must not be the reason a script breaks. `aicode runtime
// doctor` carries the same information as a structured check, for anyone who
// pipes stderr away.
func warnAboutDeprecatedConfig(writer io.Writer, cfg config.Config) {
	for _, deprecation := range cfg.Deprecations {
		fmt.Fprintf(writer, "Warning: %s\n", deprecation)
	}
}
