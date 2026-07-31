package runtimecmd

import (
	"fmt"

	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/renderer"
)

func runUsage(cfg config.Config, args []string) error {
	path, jsonOutput, err := usagePath(args)
	if err != nil {
		return err
	}
	value, err := runtimeio.FetchJSON(cfg, path)
	if err != nil {
		return err
	}
	if jsonOutput {
		renderer.PrintJSON(value)
		return nil
	}
	renderer.PrintUsageSummary(value)
	return nil
}

func usagePath(args []string) (string, bool, error) {
	jsonOutput := false
	filtered := make([]string, 0, len(args))
	for _, arg := range args {
		if arg == "--json" {
			jsonOutput = true
			continue
		}
		filtered = append(filtered, arg)
	}
	if len(filtered) == 0 {
		return "/v1/usage", jsonOutput, nil
	}
	if len(filtered) == 1 && filtered[0] == "--today" {
		return "/v1/usage?today=true", jsonOutput, nil
	}
	if len(filtered) == 2 && filtered[0] == "--session" {
		return "/v1/usage/sessions/" + filtered[1], jsonOutput, nil
	}
	return "", false, fmt.Errorf("usage: aicode runtime usage [--today|--session <session_id>] [--json]")
}
