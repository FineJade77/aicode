// Package modelscmd implements `aicode models ...`.
package modelscmd

import (
	"fmt"

	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/renderer"
)

func Run(cfg config.Config, args []string) error {
	jsonOutput := false
	if len(args) == 1 && args[0] == "--json" {
		jsonOutput = true
	} else if len(args) != 0 {
		return fmt.Errorf("用法: aicode models [--json]")
	}

	value, err := runtimeio.FetchJSON(cfg, "/v1/models/routes")
	if err != nil {
		return err
	}
	if jsonOutput {
		renderer.PrintJSON(value)
		return nil
	}
	renderer.PrintModelRoutes(value)
	return nil
}
