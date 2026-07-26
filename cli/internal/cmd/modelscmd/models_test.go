package modelscmd

import (
	"testing"
	"time"

	"github.com/FineJade77/aicode/cli/internal/config"
)

func TestParseProbeArgs(t *testing.T) {
	options, err := parseProbeArgs([]string{"--no-tools", "--model", "local-coder", "--json"})
	if err != nil {
		t.Fatal(err)
	}
	if options.tools || !options.jsonOutput || options.model != "local-coder" {
		t.Fatalf("options = %#v", options)
	}

	defaults, err := parseProbeArgs(nil)
	if err != nil {
		t.Fatal(err)
	}
	if !defaults.tools || defaults.jsonOutput || defaults.model != "" {
		t.Fatalf("defaults = %#v", defaults)
	}
}

func TestParseProbeArgsRejectsInvalidInput(t *testing.T) {
	for _, args := range [][]string{{"--model"}, {"--unknown"}} {
		if _, err := parseProbeArgs(args); err == nil {
			t.Fatalf("expected error for %#v", args)
		}
	}
}

func TestProbeTimeoutAllowsLocalModelColdStart(t *testing.T) {
	cfg := config.Default()
	cfg.OpenAICompatible.TimeoutSeconds = 60

	got := providerProbeTimeout(cfg)

	if got != 70*time.Second {
		t.Fatalf("timeout = %s", got)
	}
	cfg.OpenAICompatible.TimeoutSeconds = 1
	if got := providerProbeTimeout(cfg); got != 30*time.Second {
		t.Fatalf("minimum timeout = %s", got)
	}
}
