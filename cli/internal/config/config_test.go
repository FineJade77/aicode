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

	content := `[runtime]
url = "http://127.0.0.1:9999"
port = 9999

[models]
default = "default-model"
planner = "plan-model"
coder = "code-model"
reviewer = "review-model"
summarizer = "summary-model"

[provider.openai_compatible]
profile = "local-test"
profile_schema_version = 1
base_url = "https://api.example.com/v1"
api_key_env = "EXAMPLE_API_KEY"
auth_mode = "none"
timeout_seconds = 12.5
context_window = 16384
max_output_tokens = 2048
tool_calling = true
streaming = true
tokenizer = "chars"
chars_per_token = 4

[provider.anthropic]
base_url = "https://anthropic.example.com"
api_key_env = "ANTHROPIC_TEST_KEY"
timeout_seconds = 42

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
	if cfg.OpenAICompatible.Profile != "local-test" ||
		cfg.OpenAICompatible.ProfileSchemaVersion != 1 ||
		cfg.OpenAICompatible.AuthMode != "none" ||
		cfg.OpenAICompatible.ContextWindow != 16384 ||
		cfg.OpenAICompatible.MaxOutputTokens != 2048 ||
		!cfg.OpenAICompatible.ToolCalling ||
		!cfg.OpenAICompatible.Streaming ||
		cfg.OpenAICompatible.Tokenizer != "chars" ||
		cfg.OpenAICompatible.CharsPerToken != 4 {
		t.Fatalf("provider profile = %#v", cfg.OpenAICompatible)
	}
	if cfg.Anthropic.BaseURL != "https://anthropic.example.com" || cfg.Anthropic.APIKeyEnv != "ANTHROPIC_TEST_KEY" {
		t.Fatalf("anthropic = %#v", cfg.Anthropic)
	}
	if cfg.Anthropic.TimeoutSeconds != 42 {
		t.Fatalf("anthropic timeout = %v", cfg.Anthropic.TimeoutSeconds)
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
	if _, err := SetValue("provider.openai_compatible.profile", "ollama"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.auth_mode", "none"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.context_window", "32768"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.max_output_tokens", "4096"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.openai_compatible.tool_calling", "true"); err != nil {
		t.Fatal(err)
	}
	if _, err := SetValue("provider.anthropic.timeout_seconds", "31.5"); err != nil {
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
	if cfg.OpenAICompatible.Profile != "ollama" ||
		cfg.OpenAICompatible.AuthMode != "none" ||
		cfg.OpenAICompatible.ContextWindow != 32768 ||
		cfg.OpenAICompatible.MaxOutputTokens != 4096 ||
		!cfg.OpenAICompatible.ToolCalling {
		t.Fatalf("provider profile = %#v", cfg.OpenAICompatible)
	}
	if cfg.Anthropic.TimeoutSeconds != 31.5 {
		t.Fatalf("anthropic timeout = %v", cfg.Anthropic.TimeoutSeconds)
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
	if !strings.Contains(string(content), "[provider.anthropic]") || !strings.Contains(string(content), "timeout_seconds = 31.5") {
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
	t.Setenv("AICODE_MODEL_DEFAULT", "legacy-default")
	t.Setenv("AICODE_MODEL_PLANNER", "legacy-planner")
	t.Setenv("AICODE_MODEL_CODER", "legacy-coder")

	cfg := Default()
	cfg.Models.Reviewer = "review-model"
	cfg.OpenAICompatible.BaseURL = "https://api.example.com/v1"
	cfg.OpenAICompatible.APIKeyEnv = "EXAMPLE_API_KEY"
	cfg.OpenAICompatible.Profile = "local-test"
	cfg.OpenAICompatible.AuthMode = "none"
	cfg.OpenAICompatible.ContextWindow = 16384
	cfg.OpenAICompatible.MaxOutputTokens = 2048
	cfg.OpenAICompatible.ToolCalling = true
	cfg.OpenAICompatible.Streaming = true
	cfg.OpenAICompatible.TimeoutSeconds = 17.5
	cfg.Anthropic.TimeoutSeconds = 88
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
	if env["AICODE_OPENAI_PROFILE"] != "local-test" ||
		env["AICODE_OPENAI_AUTH_MODE"] != "none" ||
		env["AICODE_OPENAI_CONTEXT_WINDOW"] != "16384" ||
		env["AICODE_OPENAI_MAX_OUTPUT_TOKENS"] != "2048" ||
		env["AICODE_OPENAI_TOOL_CALLING"] != "true" ||
		env["AICODE_OPENAI_STREAMING"] != "true" {
		t.Fatalf("provider profile env = %#v", env)
	}
	if env["AICODE_OPENAI_TIMEOUT_SECONDS"] != "17.5" {
		t.Fatalf("AICODE_OPENAI_TIMEOUT_SECONDS = %q", env["AICODE_OPENAI_TIMEOUT_SECONDS"])
	}
	if env["AICODE_ANTHROPIC_TIMEOUT_SECONDS"] != "88" {
		t.Fatalf("AICODE_ANTHROPIC_TIMEOUT_SECONDS = %q", env["AICODE_ANTHROPIC_TIMEOUT_SECONDS"])
	}
	if !strings.Contains(env["AICODE_MODEL_PRICES_JSON"], `"openai_compatible/gpt-5"`) {
		t.Fatalf("AICODE_MODEL_PRICES_JSON = %q", env["AICODE_MODEL_PRICES_JSON"])
	}
	if !strings.Contains(env["AICODE_MODEL_PRICES_JSON"], `"input_per_1m":1.25`) {
		t.Fatalf("AICODE_MODEL_PRICES_JSON = %q", env["AICODE_MODEL_PRICES_JSON"])
	}
	for _, key := range []string{"AICODE_MODEL_DEFAULT", "AICODE_MODEL_PLANNER", "AICODE_MODEL_CODER"} {
		if containsEnvKey(cfg.RuntimeEnv(), key) {
			t.Fatalf("legacy key leaked into runtime env: %s", key)
		}
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
	if _, ok := cfg.GetValue("models.coder"); ok {
		t.Fatal("legacy models.coder should not appear in config entries")
	}
	for _, entry := range entries {
		if strings.Contains(entry.Key, "language") {
			t.Fatalf("language setting should not appear in config entries: %s", entry.Key)
		}
	}
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
	if _, ok := findDoc(docs, "provider.anthropic.timeout_seconds"); !ok {
		t.Fatal("missing anthropic timeout doc")
	}
	if _, ok := findDoc(docs, "provider.openai_compatible.auth_mode"); !ok {
		t.Fatal("missing auth mode doc")
	}
	if _, ok := findDoc(docs, "provider.openai_compatible.tool_calling"); !ok {
		t.Fatal("missing tool calling doc")
	}
	if _, ok := findDoc(docs, "models.coder"); ok {
		t.Fatal("legacy models.coder should not appear in docs")
	}
	for _, doc := range docs {
		if strings.Contains(doc.Key, "language") {
			t.Fatalf("language setting should not appear in config docs: %s", doc.Key)
		}
	}
	if strings.Contains(DefaultContent(), "language") {
		t.Fatalf("default config should not contain a language setting:\n%s", DefaultContent())
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
	if _, err := SetValue("provider.anthropic.timeout_seconds", "33"); err != nil {
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
	if env["AICODE_ANTHROPIC_TIMEOUT_SECONDS"] != "33" {
		t.Fatalf("AICODE_ANTHROPIC_TIMEOUT_SECONDS = %q", env["AICODE_ANTHROPIC_TIMEOUT_SECONDS"])
	}
}

func clearConfigEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"AICODE_RUNTIME_URL",
		"AICODE_MODEL_DEFAULT",
		"AICODE_MODEL_MAIN",
		"AICODE_MODEL_PLANNER",
		"AICODE_MODEL_CODER",
		"AICODE_MODEL_REVIEWER",
		"AICODE_MODEL_SUMMARIZER",
		"AICODE_PROVIDER_TYPE",
		"AICODE_ANTHROPIC_BASE_URL",
		"AICODE_ANTHROPIC_API_KEY_ENV",
		"AICODE_ANTHROPIC_TIMEOUT_SECONDS",
		"AICODE_OPENAI_BASE_URL",
		"AICODE_OPENAI_PROFILE",
		"AICODE_OPENAI_PROFILE_SCHEMA_VERSION",
		"AICODE_OPENAI_API_KEY_ENV",
		"AICODE_OPENAI_AUTH_MODE",
		"AICODE_OPENAI_TIMEOUT_SECONDS",
		"AICODE_OPENAI_CONTEXT_WINDOW",
		"AICODE_OPENAI_MAX_OUTPUT_TOKENS",
		"AICODE_OPENAI_TOOL_CALLING",
		"AICODE_OPENAI_STREAMING",
		"AICODE_OPENAI_TOKENIZER",
		"AICODE_OPENAI_CHARS_PER_TOKEN",
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

func containsEnvKey(env []string, target string) bool {
	for _, item := range env {
		key, _, ok := strings.Cut(item, "=")
		if ok && key == target {
			return true
		}
	}
	return false
}

// --- legacy model settings ---------------------------------------------------
//
// `models.default` / `models.planner` / `models.coder` used to be parsed into
// fields that nothing read: the value never reached the Runtime and the user was
// never told. These pin that a legacy name now either takes effect or explains
// why it did not.

func writeConfig(t *testing.T, body string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	clearConfigEnv(t)
	if err := os.WriteFile(filepath.Join(home, "config.toml"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func findDeprecation(cfg Config, key string) (Deprecation, bool) {
	for _, deprecation := range cfg.Deprecations {
		if deprecation.Key == key {
			return deprecation, true
		}
	}
	return Deprecation{}, false
}

func TestLegacyModelsDefaultFillsInForModelsMain(t *testing.T) {
	writeConfig(t, "[models]\ndefault = \"legacy-model\"\n")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.Models.Main != "legacy-model" {
		t.Fatalf("models.main = %q, want the legacy value to take effect", cfg.Models.Main)
	}
	deprecation, ok := findDeprecation(cfg, "models.default")
	if !ok {
		t.Fatalf("no deprecation reported: %#v", cfg.Deprecations)
	}
	if deprecation.Replacement != "models.main" || !strings.Contains(deprecation.Effect, "applied") {
		t.Fatalf("deprecation = %#v", deprecation)
	}
}

func TestLegacyModelsCoderFillsInForModelsMain(t *testing.T) {
	writeConfig(t, "[models]\ncoder = \"legacy-coder\"\n")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.Models.Main != "legacy-coder" {
		t.Fatalf("models.main = %q", cfg.Models.Main)
	}
}

func TestAnExplicitModelsMainBeatsALegacyName(t *testing.T) {
	writeConfig(t, "[models]\nmain = \"current-model\"\ndefault = \"legacy-model\"\n")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.Models.Main != "current-model" {
		t.Fatalf("models.main = %q, want the current name to win", cfg.Models.Main)
	}
	deprecation, ok := findDeprecation(cfg, "models.default")
	if !ok {
		t.Fatalf("a legacy key that changed nothing must still be reported: %#v", cfg.Deprecations)
	}
	if !strings.Contains(deprecation.Effect, "ignored") {
		t.Fatalf("deprecation = %#v", deprecation)
	}
}

func TestOnlyOneLegacyNameCanWin(t *testing.T) {
	// Both present and no `models.main`: `coder` matches the Runtime's own
	// fallback order, so it wins and `default` reports itself as ignored.
	writeConfig(t, "[models]\ndefault = \"legacy-default\"\ncoder = \"legacy-coder\"\n")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.Models.Main != "legacy-coder" {
		t.Fatalf("models.main = %q", cfg.Models.Main)
	}
	losing, ok := findDeprecation(cfg, "models.default")
	if !ok || !strings.Contains(losing.Effect, "ignored") {
		t.Fatalf("models.default = %#v (all: %#v)", losing, cfg.Deprecations)
	}
}

func TestLegacyModelsPlannerHasNoReplacement(t *testing.T) {
	writeConfig(t, "[models]\nplanner = \"plan-model\"\n")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	deprecation, ok := findDeprecation(cfg, "models.planner")
	if !ok {
		t.Fatalf("no deprecation reported: %#v", cfg.Deprecations)
	}
	if deprecation.Replacement != "" {
		t.Fatalf("planner has no current equivalent, so nothing may be suggested: %#v", deprecation)
	}
	// It must not quietly become the main model: there is no planner route.
	if cfg.Models.Main == "plan-model" {
		t.Fatal("models.planner must not be adopted as models.main")
	}
}

func TestALegacyEnvVariableIsReportedToo(t *testing.T) {
	// The env path is the one a user is least likely to remember setting.
	writeConfig(t, "[models]\n")
	t.Setenv("AICODE_MODEL_DEFAULT", "env-legacy-model")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if cfg.Models.Main != "env-legacy-model" {
		t.Fatalf("models.main = %q", cfg.Models.Main)
	}
	if _, ok := findDeprecation(cfg, "models.default"); !ok {
		t.Fatalf("no deprecation reported: %#v", cfg.Deprecations)
	}
}

func TestACurrentConfigurationReportsNothing(t *testing.T) {
	writeConfig(t, "[models]\nmain = \"current-model\"\nreviewer = \"review-model\"\n")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if len(cfg.Deprecations) != 0 {
		t.Fatalf("deprecations = %#v, want none", cfg.Deprecations)
	}
}

func TestAdoptedLegacyValueReachesTheRuntime(t *testing.T) {
	// The whole point: the Runtime only ever reads AICODE_MODEL_MAIN, so a
	// legacy name that stops short of it has changed nothing.
	writeConfig(t, "[models]\ndefault = \"legacy-model\"\n")

	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	env := envMap(cfg.RuntimeEnv())

	if env["AICODE_MODEL_MAIN"] != "legacy-model" {
		t.Fatalf("AICODE_MODEL_MAIN = %q", env["AICODE_MODEL_MAIN"])
	}
}
