package runtimecmd

import (
	"fmt"
	"net/url"
	"time"

	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/renderer"
)

func runModels(cfg config.Config, args []string) error {
	if len(args) > 0 && args[0] == "probe" {
		return runProbe(cfg, args[1:])
	}
	jsonOutput := false
	if len(args) == 1 && args[0] == "--json" {
		jsonOutput = true
	} else if len(args) != 0 {
		return fmt.Errorf("usage: aicode runtime models [--json] | aicode runtime models probe [--no-tools] [--model <name>] [--routes] [--json]")
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

func runProbe(cfg config.Config, args []string) error {
	options, err := parseProbeArgs(args)
	if err != nil {
		return err
	}

	params := url.Values{}
	if options.routes {
		// Each route is probed with the tool support its own capability
		// declares, so a global --no-tools would describe something the routes
		// do not do.
		if options.model != "" {
			return fmt.Errorf("--routes probes every configured route; it cannot be combined with --model")
		}
		params.Set("routes", "true")
	} else {
		params.Set("tools", fmt.Sprintf("%t", options.tools))
		if options.model != "" {
			params.Set("model", options.model)
		}
	}
	value, err := runtimeio.FetchJSONWithTimeout(cfg, "/v1/models/probe?"+params.Encode(), providerProbeTimeout(cfg))
	if err != nil {
		return err
	}
	if options.jsonOutput {
		renderer.PrintJSON(value)
	} else if options.routes {
		renderer.PrintRouteProbe(value)
	} else {
		renderer.PrintModelProbe(value)
	}
	if root, ok := value.(map[string]any); ok && root["status"] == "error" {
		return fmt.Errorf("Provider Profile probe failed")
	}
	return nil
}

type probeOptions struct {
	jsonOutput bool
	tools      bool
	model      string
	routes     bool
}

func parseProbeArgs(args []string) (probeOptions, error) {
	options := probeOptions{tools: true}
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "--json":
			options.jsonOutput = true
		case "--no-tools":
			options.tools = false
		case "--routes":
			options.routes = true
		case "--model":
			index++
			if index >= len(args) || args[index] == "" {
				return options, fmt.Errorf("--model requires a model name")
			}
			options.model = args[index]
		default:
			return options, fmt.Errorf("usage: aicode runtime models probe [--no-tools] [--model <name>] [--routes] [--json]")
		}
	}
	return options, nil
}

func providerProbeTimeout(cfg config.Config) time.Duration {
	timeout := time.Duration(cfg.OpenAICompatible.TimeoutSeconds*float64(time.Second)) + 10*time.Second
	if timeout < 30*time.Second {
		return 30 * time.Second
	}
	return timeout
}
