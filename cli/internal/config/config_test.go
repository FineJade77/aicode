package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadReadsModelAndProviderConfig(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)

	content := `[ui]
language = "en-US"

[runtime]
url = "http://127.0.0.1:9999"
port = 9999

[models]
default = "default-model"
planner = "plan-model"
coder = "code-model"
reviewer = "review-model"
summarizer = "summary-model"

[provider.openai_compatible]
base_url = "https://api.example.com/v1"
api_key_env = "EXAMPLE_API_KEY"
timeout_seconds = 12.5
`
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.UI.Language != "en-US" {
		t.Fatalf("language = %q", cfg.UI.Language)
	}
	if cfg.Runtime.URL != "http://127.0.0.1:9999" || cfg.Runtime.Port != 9999 {
		t.Fatalf("runtime = %#v", cfg.Runtime)
	}
	if cfg.Models.Planner != "plan-model" || cfg.Models.Reviewer != "review-model" || cfg.Models.Summarizer != "summary-model" {
		t.Fatalf("models = %#v", cfg.Models)
	}
	if cfg.OpenAICompatible.BaseURL != "https://api.example.com/v1" || cfg.OpenAICompatible.APIKeyEnv != "EXAMPLE_API_KEY" {
		t.Fatalf("provider = %#v", cfg.OpenAICompatible)
	}
	if cfg.OpenAICompatible.TimeoutSeconds != 12.5 {
		t.Fatalf("timeout = %v", cfg.OpenAICompatible.TimeoutSeconds)
	}
}

func TestSetValueSupportsModelRoutes(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)

	if _, err := SetValue("models.reviewer", "review-model"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.base_url", "https://api.example.com/v1"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.timeout_seconds", "7.5"); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.Models.Reviewer != "review-model" {
		t.Fatalf("reviewer = %q", cfg.Models.Reviewer)
	}
	if cfg.OpenAICompatible.BaseURL != "https://api.example.com/v1" {
		t.Fatalf("base_url = %q", cfg.OpenAICompatible.BaseURL)
	}
	if cfg.OpenAICompatible.TimeoutSeconds != 7.5 {
		t.Fatalf("timeout = %v", cfg.OpenAICompatible.TimeoutSeconds)
	}

	content, err := os.ReadFile(filepath.Join(home, "config.toml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(content), "timeout_seconds = 7.5") {
		t.Fatalf("config content = %s", content)
	}
}

func TestRuntimeEnvIncludesModelAndProviderConfig(t *testing.T) {
	cfg := Default()
	cfg.Models.Reviewer = "review-model"
	cfg.OpenAICompatible.BaseURL = "https://api.example.com/v1"
	cfg.OpenAICompatible.APIKeyEnv = "EXAMPLE_API_KEY"
	cfg.OpenAICompatible.TimeoutSeconds = 17.5

	env := envMap(cfg.RuntimeEnv())

	if env["AICODE_MODEL_REVIEWER"] != "review-model" {
		t.Fatalf("AICODE_MODEL_REVIEWER = %q", env["AICODE_MODEL_REVIEWER"])
	}
	if env["AICODE_OPENAI_BASE_URL"] != "https://api.example.com/v1" {
		t.Fatalf("AICODE_OPENAI_BASE_URL = %q", env["AICODE_OPENAI_BASE_URL"])
	}
	if env["AICODE_OPENAI_API_KEY_ENV"] != "EXAMPLE_API_KEY" {
		t.Fatalf("AICODE_OPENAI_API_KEY_ENV = %q", env["AICODE_OPENAI_API_KEY_ENV"])
	}
	if env["AICODE_OPENAI_TIMEOUT_SECONDS"] != "17.5" {
		t.Fatalf("AICODE_OPENAI_TIMEOUT_SECONDS = %q", env["AICODE_OPENAI_TIMEOUT_SECONDS"])
	}
}

func clearConfigEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"AICODE_RUNTIME_URL",
		"AICODE_DEFAULT_LANGUAGE",
		"AICODE_MODEL_DEFAULT",
		"AICODE_MODEL_PLANNER",
		"AICODE_MODEL_CODER",
		"AICODE_MODEL_REVIEWER",
		"AICODE_MODEL_SUMMARIZER",
		"AICODE_OPENAI_BASE_URL",
		"AICODE_OPENAI_API_KEY_ENV",
		"AICODE_OPENAI_TIMEOUT_SECONDS",
	} {
		t.Setenv(key, "")
	}
}

func envMap(env []string) map[string]string {
	values := map[string]string{}
	for _, item := range env {
		key, value, ok := strings.Cut(item, "=")
		if ok {
			values[key] = value
		}
	}
	return values
}
