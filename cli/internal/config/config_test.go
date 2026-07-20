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

[pricing.openai_compatible.gpt-5]
input_per_1m = 1.25
output_per_1m = 10
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
	price := cfg.Pricing["openai_compatible/gpt-5"]
	if price.InputPer1M != 1.25 || price.OutputPer1M != 10 {
		t.Fatalf("price = %#v", price)
	}
}

func TestSetValueSupportsModelRoutes(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)

	if _, err := SetValue("runtime.port", "9999"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("ui.style", "codex"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("models.reviewer", "review-model"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.base_url", "https://api.example.com/v1"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.timeout_seconds", "7.5"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("pricing.openai_compatible.gpt-5.input_per_1m", "1.25"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("pricing.openai_compatible.gpt-5.output_per_1m", "10"); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.Models.Reviewer != "review-model" {
		t.Fatalf("reviewer = %q", cfg.Models.Reviewer)
	}
	if cfg.Runtime.Port != 9999 {
		t.Fatalf("port = %d", cfg.Runtime.Port)
	}
	if cfg.UI.Style != "codex" {
		t.Fatalf("style = %q", cfg.UI.Style)
	}
	if cfg.OpenAICompatible.BaseURL != "https://api.example.com/v1" {
		t.Fatalf("base_url = %q", cfg.OpenAICompatible.BaseURL)
	}
	if cfg.OpenAICompatible.TimeoutSeconds != 7.5 {
		t.Fatalf("timeout = %v", cfg.OpenAICompatible.TimeoutSeconds)
	}
	price := cfg.Pricing["openai_compatible/gpt-5"]
	if price.InputPer1M != 1.25 || price.OutputPer1M != 10 {
		t.Fatalf("price = %#v", price)
	}

	content, err := os.ReadFile(filepath.Join(home, "config.toml"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(content), "timeout_seconds = 7.5") {
		t.Fatalf("config content = %s", content)
	}
	if !strings.Contains(string(content), "port = 9999") {
		t.Fatalf("config content = %s", content)
	}
	if !strings.Contains(string(content), "[pricing.openai_compatible.gpt-5]") {
		t.Fatalf("config content = %s", content)
	}
	if !strings.Contains(string(content), "input_per_1m = 1.25") || !strings.Contains(string(content), "output_per_1m = 10") {
		t.Fatalf("config content = %s", content)
	}
}

func TestUnsetValueRemovesExplicitConfig(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)

	content := `[models]
reviewer = "custom-reviewer"
summarizer = "custom-summary"
`
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	path, removed, err := UnsetValue("models.reviewer")
	if err != nil {
		t.Fatal(err)
	}
	if !removed {
		t.Fatal("expected removed")
	}
	if path != filepath.Join(home, "config.toml") {
		t.Fatalf("path = %q", path)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Models.Reviewer != Default().Models.Reviewer {
		t.Fatalf("reviewer = %q", cfg.Models.Reviewer)
	}
	if cfg.Models.Summarizer != "custom-summary" {
		t.Fatalf("summarizer = %q", cfg.Models.Summarizer)
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "reviewer") {
		t.Fatalf("config content = %s", raw)
	}
	if !strings.Contains(string(raw), `summarizer = "custom-summary"`) {
		t.Fatalf("config content = %s", raw)
	}
}

func TestUnsetValueRemovesEmptyPricingSection(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)

	content := `[models]
reviewer = "custom-reviewer"

[pricing.openai_compatible.gpt-5]
input_per_1m = 1.25
`
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}

	path, removed, err := UnsetValue("pricing.openai_compatible.gpt-5.input_per_1m")
	if err != nil {
		t.Fatal(err)
	}
	if !removed {
		t.Fatal("expected removed")
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "pricing.openai_compatible.gpt-5") || strings.Contains(string(raw), "input_per_1m") {
		t.Fatalf("config content = %s", raw)
	}
	if !strings.Contains(string(raw), `reviewer = "custom-reviewer"`) {
		t.Fatalf("config content = %s", raw)
	}
}

func TestUnsetValueNoopWhenMissing(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)

	path, removed, err := UnsetValue("models.reviewer")
	if err != nil {
		t.Fatal(err)
	}
	if removed {
		t.Fatal("expected noop")
	}
	if path != filepath.Join(home, "config.toml") {
		t.Fatalf("path = %q", path)
	}
}

func TestRuntimeEnvIncludesModelAndProviderConfig(t *testing.T) {
	cfg := Default()
	cfg.Models.Reviewer = "review-model"
	cfg.OpenAICompatible.BaseURL = "https://api.example.com/v1"
	cfg.OpenAICompatible.APIKeyEnv = "EXAMPLE_API_KEY"
	cfg.OpenAICompatible.TimeoutSeconds = 17.5
	cfg.Pricing["openai_compatible/gpt-5"] = ModelPriceConfig{InputPer1M: 1.25, OutputPer1M: 10}

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
	if !strings.Contains(env["AICODE_MODEL_PRICES_JSON"], `"openai_compatible/gpt-5"`) {
		t.Fatalf("AICODE_MODEL_PRICES_JSON = %q", env["AICODE_MODEL_PRICES_JSON"])
	}
	if !strings.Contains(env["AICODE_MODEL_PRICES_JSON"], `"input_per_1m":1.25`) {
		t.Fatalf("AICODE_MODEL_PRICES_JSON = %q", env["AICODE_MODEL_PRICES_JSON"])
	}
}

func TestEntriesAndGetValueIncludePricing(t *testing.T) {
	cfg := Default()
	cfg.Pricing["openai_compatible/gpt-5"] = ModelPriceConfig{InputPer1M: 1.25, OutputPer1M: 10}
	cfg.Pricing["stub/stub"] = ModelPriceConfig{InputPer1M: 1, OutputPer1M: 1}

	value, ok := cfg.GetValue("pricing.openai_compatible.gpt-5.input_per_1m")
	if !ok || value != "1.25" {
		t.Fatalf("value = %q ok = %v", value, ok)
	}
	value, ok = cfg.GetValue("pricing.stub.stub.output_per_1m")
	if !ok || value != "1" {
		t.Fatalf("value = %q ok = %v", value, ok)
	}
	if _, ok := cfg.GetValue("pricing.openai_compatible.gpt-5.unknown"); ok {
		t.Fatal("expected unknown key")
	}

	entries := cfg.Entries()
	assertEntryOrder(t, entries, "pricing.openai_compatible.gpt-5.input_per_1m", "pricing.stub.stub.input_per_1m")
}

func TestKeyDocsIncludeCoreAndPricingKeys(t *testing.T) {
	docs := KeyDocs()

	reviewer, ok := findDoc(docs, "models.reviewer")
	if !ok {
		t.Fatal("missing models.reviewer")
	}
	if reviewer.Default != "gpt-5" {
		t.Fatalf("reviewer default = %q", reviewer.Default)
	}
	if reviewer.Env != "AICODE_MODEL_REVIEWER" {
		t.Fatalf("reviewer env = %q", reviewer.Env)
	}
	if _, ok := findDoc(docs, "pricing.<provider>.<model>.input_per_1m"); !ok {
		t.Fatal("missing pricing input doc")
	}
	if _, ok := findDoc(docs, "runtime.port"); !ok {
		t.Fatal("missing runtime.port")
	}
}

func TestModelsMainAndProviderTypeKeys(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)

	if _, err := SetValue("models.main", "m-x"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.type", "anthropic"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.anthropic.api_key_env", "MY_KEY"); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	env := envMap(cfg.RuntimeEnv())
	if env["AICODE_MODEL_MAIN"] != "m-x" {
		t.Fatalf("AICODE_MODEL_MAIN = %q", env["AICODE_MODEL_MAIN"])
	}
	if env["AICODE_PROVIDER_TYPE"] != "anthropic" {
		t.Fatalf("AICODE_PROVIDER_TYPE = %q", env["AICODE_PROVIDER_TYPE"])
	}
	if env["AICODE_ANTHROPIC_API_KEY_ENV"] != "MY_KEY" {
		t.Fatalf("AICODE_ANTHROPIC_API_KEY_ENV = %q", env["AICODE_ANTHROPIC_API_KEY_ENV"])
	}
}

func clearConfigEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"AICODE_RUNTIME_URL",
		"AICODE_DEFAULT_LANGUAGE",
		"AICODE_MODEL_DEFAULT",
		"AICODE_MODEL_MAIN",
		"AICODE_MODEL_PLANNER",
		"AICODE_MODEL_CODER",
		"AICODE_MODEL_REVIEWER",
		"AICODE_MODEL_SUMMARIZER",
		"AICODE_PROVIDER_TYPE",
		"AICODE_ANTHROPIC_BASE_URL",
		"AICODE_ANTHROPIC_API_KEY_ENV",
		"AICODE_OPENAI_BASE_URL",
		"AICODE_OPENAI_API_KEY_ENV",
		"AICODE_OPENAI_TIMEOUT_SECONDS",
		"AICODE_MODEL_PRICES_JSON",
	} {
		t.Setenv(key, "")
	}
}

func findDoc(docs []KeyDoc, key string) (KeyDoc, bool) {
	for _, doc := range docs {
		if doc.Key == key {
			return doc, true
		}
	}
	return KeyDoc{}, false
}

func assertEntryOrder(t *testing.T, entries []Entry, before string, after string) {
	t.Helper()
	beforeIndex := -1
	afterIndex := -1
	for i, entry := range entries {
		if entry.Key == before {
			beforeIndex = i
		}
		if entry.Key == after {
			afterIndex = i
		}
	}
	if beforeIndex < 0 || afterIndex < 0 || beforeIndex >= afterIndex {
		t.Fatalf("unexpected order for %q and %q in %#v", before, after, entries)
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
