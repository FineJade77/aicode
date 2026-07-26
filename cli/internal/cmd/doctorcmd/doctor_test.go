package doctorcmd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
)

func TestBuildReportHealthy(t *testing.T) {
	cfg := config.Default()
	deps := healthyDependencies()

	report := buildReport(cfg, deps)

	if report.Status != StatusOK {
		t.Fatalf("status = %q, checks = %#v", report.Status, report.Checks)
	}
	if report.CLIVersion != "0.1.0" {
		t.Fatalf("cli_version = %q", report.CLIVersion)
	}
	if len(report.Checks) != 6 {
		t.Fatalf("checks = %d, want 6", len(report.Checks))
	}
	for _, check := range report.Checks {
		if check.Status != StatusOK {
			t.Fatalf("%s status = %q: %#v", check.Name, check.Status, check)
		}
	}
	provider := findCheck(t, report, "provider")
	if provider.Details["api_key_source"] != "AICODE_OPENAI_API_KEY" {
		t.Fatalf("provider details = %#v", provider.Details)
	}
}

func TestBuildReportDetectsVersionMismatchAndOccupiedPort(t *testing.T) {
	cfg := config.Default()
	deps := healthyDependencies()
	deps.resolveRuntime = func() (daemon.RuntimeInstallation, error) {
		return daemon.RuntimeInstallation{
			RuntimeDir: "/runtime",
			Python:     "/venv/bin/python",
			Version:    "0.2.0",
			Source:     "install-manifest",
		}, nil
	}
	deps.daemonStatus = func(context.Context, string) (map[string]any, error) {
		return nil, errors.New("connection reset")
	}
	deps.portOpen = func(string, time.Duration) (bool, error) {
		return true, nil
	}

	report := buildReport(cfg, deps)

	if report.Status != StatusError {
		t.Fatalf("status = %q, checks = %#v", report.Status, report.Checks)
	}
	if got := findCheck(t, report, "version").Status; got != StatusError {
		t.Fatalf("version status = %q", got)
	}
	port := findCheck(t, report, "port")
	if port.Status != StatusError || !strings.Contains(port.Summary, "端口已被占用") {
		t.Fatalf("port check = %#v", port)
	}
}

func TestBuildReportWarnsForOptionalAndInactiveServices(t *testing.T) {
	cfg := config.Default()
	deps := healthyDependencies()
	deps.daemonStatus = func(context.Context, string) (map[string]any, error) {
		return nil, errors.New("connection refused")
	}
	deps.portOpen = func(string, time.Duration) (bool, error) {
		return false, errors.New("connection refused")
	}
	deps.lookupEnv = func(string) (string, bool) {
		return "", false
	}
	deps.lookPath = func(string) (string, error) {
		return "", errors.New("not found")
	}

	report := buildReport(cfg, deps)

	if report.Status != StatusWarn {
		t.Fatalf("status = %q, checks = %#v", report.Status, report.Checks)
	}
	for _, name := range []string{"port", "provider", "docker"} {
		if got := findCheck(t, report, name).Status; got != StatusWarn {
			t.Fatalf("%s status = %q", name, got)
		}
	}
}

func TestProviderCheckAcceptsNoAuthLocalProfileWithoutFakeKey(t *testing.T) {
	cfg := config.Default()
	cfg.OpenAICompatible.Profile = "ollama"
	cfg.OpenAICompatible.BaseURL = "http://127.0.0.1:11434/v1"
	cfg.OpenAICompatible.AuthMode = "none"
	cfg.OpenAICompatible.APIKeyEnv = ""
	deps := healthyDependencies()
	deps.lookupEnv = func(string) (string, bool) { return "", false }

	check := checkProvider(cfg, deps)

	if check.Status != StatusOK || check.Details["configured"] != true {
		t.Fatalf("provider check = %#v", check)
	}
	if !strings.Contains(check.Summary, "no-auth") {
		t.Fatalf("summary = %q", check.Summary)
	}
}

func TestProviderCheckRejectsInvalidProfileCapabilities(t *testing.T) {
	cfg := config.Default()
	cfg.OpenAICompatible.AuthMode = "invalid"
	check := checkProvider(cfg, healthyDependencies())
	if check.Status != StatusError || !strings.Contains(check.Summary, "auth_mode") {
		t.Fatalf("provider check = %#v", check)
	}

	cfg = config.Default()
	cfg.OpenAICompatible.MaxOutputTokens = cfg.OpenAICompatible.ContextWindow
	check = checkProvider(cfg, healthyDependencies())
	if check.Status != StatusError || !strings.Contains(check.Summary, "capability") {
		t.Fatalf("provider check = %#v", check)
	}
}

func TestBuildReportRejectsOldPythonAndMismatchedRuntimePort(t *testing.T) {
	cfg := config.Default()
	cfg.Runtime.Port = 9999
	deps := healthyDependencies()
	deps.runCommand = func(_ context.Context, name string, _ ...string) (string, error) {
		if name == "/venv/bin/python" {
			return "3.10.14", nil
		}
		return "27.1.0", nil
	}

	report := buildReport(cfg, deps)

	if got := findCheck(t, report, "python").Status; got != StatusError {
		t.Fatalf("python status = %q", got)
	}
	port := findCheck(t, report, "port")
	if port.Status != StatusError || !strings.Contains(port.Summary, "不一致") {
		t.Fatalf("port check = %#v", port)
	}
}

func TestRenderersProduceMachineAndHumanReadableOutput(t *testing.T) {
	report := buildReport(config.Default(), healthyDependencies())

	var jsonOutput bytes.Buffer
	if err := renderJSON(&jsonOutput, report); err != nil {
		t.Fatal(err)
	}
	var decoded Report
	if err := json.Unmarshal(jsonOutput.Bytes(), &decoded); err != nil {
		t.Fatalf("invalid JSON: %v\n%s", err, jsonOutput.String())
	}
	if strings.Contains(jsonOutput.String(), "secret-not-rendered") {
		t.Fatal("doctor JSON must not expose provider API keys")
	}
	if decoded.Status != StatusOK || len(decoded.Checks) != 6 {
		t.Fatalf("decoded = %#v", decoded)
	}

	var humanOutput bytes.Buffer
	if err := renderHuman(&humanOutput, report); err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{"aicode doctor", "[OK] installation", "[OK] provider"} {
		if !strings.Contains(humanOutput.String(), expected) {
			t.Fatalf("human output missing %q:\n%s", expected, humanOutput.String())
		}
	}
}

func TestParseArgs(t *testing.T) {
	if jsonOutput, err := parseArgs([]string{"--json"}); err != nil || !jsonOutput {
		t.Fatalf("parseArgs --json = %v, %v", jsonOutput, err)
	}
	if _, err := parseArgs([]string{"--fix"}); err == nil {
		t.Fatal("expected unsupported option to fail")
	}
}

func healthyDependencies() dependencies {
	return dependencies{
		resolveRuntime: func() (daemon.RuntimeInstallation, error) {
			return daemon.RuntimeInstallation{
				RuntimeDir: "/runtime",
				Python:     "/venv/bin/python",
				Version:    "0.1.0",
				Source:     "install-manifest",
			}, nil
		},
		daemonStatus: func(context.Context, string) (map[string]any, error) {
			return map[string]any{
				"status":  "ok",
				"version": "0.1.0",
				"pid":     float64(123),
			}, nil
		},
		runCommand: func(_ context.Context, name string, _ ...string) (string, error) {
			if name == "/venv/bin/python" {
				return "3.11.9", nil
			}
			return "27.1.0", nil
		},
		lookPath: func(string) (string, error) {
			return "/usr/local/bin/docker", nil
		},
		portOpen: func(string, time.Duration) (bool, error) {
			return false, errors.New("port check should not be called for a healthy daemon")
		},
		lookupEnv: func(key string) (string, bool) {
			if key == "AICODE_OPENAI_API_KEY" {
				return "secret-not-rendered", true
			}
			return "", false
		},
		cliVersion: "0.1.0",
	}
}

func findCheck(t *testing.T, report Report, name string) Check {
	t.Helper()
	for _, check := range report.Checks {
		if check.Name == name {
			return check
		}
	}
	t.Fatalf("check %q not found", name)
	return Check{}
}
