// Package configcmd manages global CLI and Runtime configuration.
package configcmd

import (
	"fmt"
	"os"
	"text/tabwriter"

	"github.com/FineJade77/aicode/cli/internal/cmd/projectcmd"
	"github.com/FineJade77/aicode/cli/internal/config"
)

const HelpText = `Usage:
  aicode config init
  aicode config show
  aicode config list
  aicode config get <key>
  aicode config set <key> <value>
  aicode config unset <key>
  aicode config docs

Project-scoped settings live under ` + "`aicode project`" + `.
`

func Run(cfg config.Config, args []string) error {
	if len(args) == 0 || isHelp(args[0]) {
		fmt.Print(HelpText)
		return nil
	}

	switch args[0] {
	case "init":
		path, err := config.Init()
		if err != nil {
			return err
		}
		fmt.Printf("Created configuration file: %s\n", path)
		return nil
	case "show":
		content, path, err := config.ReadRaw()
		if err != nil {
			return err
		}
		fmt.Printf("# %s\n%s", path, content)
		return nil
	case "list":
		if len(args) != 1 {
			return fmt.Errorf("usage: aicode config list")
		}
		return runConfigList(cfg)
	case "docs":
		if len(args) != 1 {
			return fmt.Errorf("usage: aicode config docs")
		}
		return runConfigDocs()
	case "get":
		if len(args) != 2 {
			return fmt.Errorf("usage: aicode config get <key>")
		}
		return runConfigGet(cfg, args[1])
	case "set":
		if len(args) != 3 {
			return fmt.Errorf("usage: aicode config set <key> <value>")
		}
		path, err := config.SetValue(args[1], args[2])
		if err != nil {
			return err
		}
		fmt.Printf("Updated %s = %s (%s)\n", args[1], args[2], path)
		return nil
	case "unset":
		if len(args) != 2 {
			return fmt.Errorf("usage: aicode config unset <key>")
		}
		path, removed, err := config.UnsetValue(args[1])
		if err != nil {
			return err
		}
		if removed {
			fmt.Printf("Removed %s (%s)\n", args[1], path)
			return nil
		}
		fmt.Printf("%s is not explicitly set in the user configuration (%s)\n", args[1], path)
		return nil
	// Hidden compatibility aliases for project-scoped configuration.
	case "protected":
		return projectcmd.RunProtected(args[1:])
	case "review":
		return projectcmd.RunReview(cfg, args[1:])
	case "test":
		return projectcmd.RunTestCommand(args[1:])
	case "workspace":
		return projectcmd.RunWorkspace(args[1:])
	default:
		return fmt.Errorf("unknown config command: %s", args[0])
	}
}

func runConfigList(cfg config.Config) error {
	fmt.Println("Effective Config")
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "KEY\tVALUE")
	for _, entry := range cfg.Entries() {
		fmt.Fprintf(writer, "%s\t%s\n", entry.Key, entry.Value)
	}
	return writer.Flush()
}

func runConfigDocs() error {
	fmt.Println("Config Keys")
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "KEY\tDEFAULT\tENV\tDESCRIPTION")
	for _, doc := range config.KeyDocs() {
		fmt.Fprintf(writer, "%s\t%s\t%s\t%s\n", doc.Key, doc.Default, doc.Env, doc.Description)
	}
	return writer.Flush()
}

func runConfigGet(cfg config.Config, key string) error {
	value, ok := cfg.GetValue(key)
	if !ok {
		return fmt.Errorf("unknown configuration key: %s; run aicode config docs for supported keys", key)
	}
	fmt.Printf("%s = %s\n", key, value)
	return nil
}

func isHelp(value string) bool {
	return value == "help" || value == "--help" || value == "-h"
}
