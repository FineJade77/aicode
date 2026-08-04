package main

import (
	"bytes"
	"reflect"
	"strings"
	"testing"

	"github.com/FineJade77/aicode/cli/internal/config"
)

func TestParseGlobalArgsSupportsSandbox(t *testing.T) {
	options, args, err := parseGlobalArgs([]string{"--sandbox", "docker", "lint"})
	if err != nil {
		t.Fatal(err)
	}
	if options.Sandbox != "docker" {
		t.Fatalf("sandbox = %q", options.Sandbox)
	}
	if !reflect.DeepEqual(args, []string{"lint"}) {
		t.Fatalf("args = %#v", args)
	}
}

func TestParseGlobalArgsReportsMissingSandboxValue(t *testing.T) {
	if _, _, err := parseGlobalArgs([]string{"--sandbox"}); err == nil {
		t.Fatal("expected error")
	}
}

func TestNormalizeLegacyArgs(t *testing.T) {
	tests := []struct {
		name string
		args []string
		want []string
	}{
		{name: "sessions list", args: []string{"sessions", "--limit", "10"}, want: []string{"session", "list", "--limit", "10"}},
		{name: "sessions prune", args: []string{"sessions", "prune", "--max-age-days", "7"}, want: []string{"session", "prune", "--max-age-days", "7"}},
		{name: "resume inspect", args: []string{"resume", "--last"}, want: []string{"session", "show", "--last"}},
		{name: "resume message", args: []string{"resume", "sess_1", "continue"}, want: []string{"session", "resume", "sess_1", "continue"}},
		{name: "cancel", args: []string{"cancel", "--last"}, want: []string{"session", "cancel", "--last"}},
		{name: "daemon", args: []string{"daemon", "status"}, want: []string{"runtime", "status"}},
		{name: "doctor", args: []string{"doctor", "--json"}, want: []string{"runtime", "doctor", "--json"}},
		{name: "models", args: []string{"models", "probe"}, want: []string{"runtime", "models", "probe"}},
		{name: "usage", args: []string{"usage", "--today"}, want: []string{"runtime", "usage", "--today"}},
		{name: "trust", args: []string{"trust", "list"}, want: []string{"project", "trust", "list"}},
		{name: "review rules", args: []string{"review-rules"}, want: []string{"project", "review-rules-json"}},
		{name: "repl", args: []string{"repl"}, want: []string{"chat"}},
		{name: "task preset", args: []string{"review"}, want: []string{"task", "review"}},
		{name: "explain", args: []string{"explain", "main.go"}, want: []string{"task", "explain", "main.go"}},
		{name: "canonical", args: []string{"session", "list"}, want: []string{"session", "list"}},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := normalizeLegacyArgs(test.args); !reflect.DeepEqual(got, test.want) {
				t.Fatalf("normalizeLegacyArgs(%#v) = %#v, want %#v", test.args, got, test.want)
			}
		})
	}
}

func TestRootHelpShowsCategoriesAndHidesLegacyCommands(t *testing.T) {
	for _, category := range []string{"task", "session", "runtime", "project", "config"} {
		if !strings.Contains(rootHelpText, category) {
			t.Fatalf("root help is missing category %q", category)
		}
	}
	for _, legacy := range []string{"aicode daemon", "aicode sessions", "aicode resume", "aicode review-rules"} {
		if strings.Contains(rootHelpText, legacy) {
			t.Fatalf("root help exposes legacy command %q", legacy)
		}
	}
}

func TestCategoryHelpRequest(t *testing.T) {
	tests := []struct {
		args     []string
		category string
		ok       bool
	}{
		{args: []string{"session"}, category: "session", ok: true},
		{args: []string{"runtime", "--help"}, category: "runtime", ok: true},
		{args: []string{"chat", "help"}, category: "chat", ok: true},
		{args: []string{"chat"}, ok: false},
		{args: []string{"session", "list"}, ok: false},
		{args: []string{"legacy"}, ok: false},
	}
	for _, test := range tests {
		category, ok := categoryHelpRequest(test.args)
		if category != test.category || ok != test.ok {
			t.Fatalf("categoryHelpRequest(%#v) = (%q, %t), want (%q, %t)", test.args, category, ok, test.category, test.ok)
		}
	}
}

func TestDeprecationWarningsGoToStderrNotStdout(t *testing.T) {
	// stdout carries `--json` payloads. A migration notice printed there would
	// make the fix for one problem the cause of another.
	cfg := config.Config{Deprecations: []config.Deprecation{
		{Key: "models.default", Replacement: "models.main", Effect: `applied as models.main = "legacy"; rename it`},
	}}

	var buffer bytes.Buffer
	warnAboutDeprecatedConfig(&buffer, cfg)

	output := buffer.String()
	if !strings.Contains(output, "models.default") || !strings.Contains(output, "models.main") {
		t.Fatalf("warning = %q", output)
	}
	if !strings.HasPrefix(output, "Warning: ") {
		t.Fatalf("warning = %q", output)
	}
}

func TestNoWarningForACurrentConfig(t *testing.T) {
	var buffer bytes.Buffer
	warnAboutDeprecatedConfig(&buffer, config.Default())

	if buffer.Len() != 0 {
		t.Fatalf("unexpected output: %q", buffer.String())
	}
}
