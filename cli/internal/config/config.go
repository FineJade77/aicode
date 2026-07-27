package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
)

type Config struct {
	UI               UIConfig
	Runtime          RuntimeConfig
	Models           ModelsConfig
	Provider         ProviderConfig
	Anthropic        AnthropicConfig
	OpenAICompatible OpenAICompatibleConfig
	Pricing          map[string]ModelPriceConfig
}

type UIConfig struct {
	Style string
}

const DefaultLanguage = "en-US"

type RuntimeConfig struct {
	URL  string
	Port int
}

type ModelsConfig struct {
	Default    string
	Main       string
	Planner    string
	Coder      string
	Reviewer   string
	Summarizer string
}

type ProviderConfig struct {
	Type string
}

type AnthropicConfig struct {
	BaseURL        string
	APIKeyEnv      string
	TimeoutSeconds float64
}

type OpenAICompatibleConfig struct {
	Profile              string
	ProfileSchemaVersion int
	BaseURL              string
	APIKeyEnv            string
	AuthMode             string
	TimeoutSeconds       float64
	ContextWindow        int
	MaxOutputTokens      int
	ToolCalling          bool
	Streaming            bool
	Tokenizer            string
	CharsPerToken        float64
}

type ModelPriceConfig struct {
	InputPer1M  float64 `json:"input_per_1m"`
	OutputPer1M float64 `json:"output_per_1m"`
}

type Entry struct {
	Key   string
	Value string
}

type KeyDoc struct {
	Key         string
	Default     string
	Env         string
	Description string
}

func Default() Config {
	return Config{
		UI: UIConfig{
			Style: "codex",
		},
		Runtime: RuntimeConfig{
			URL:  "http://127.0.0.1:8765",
			Port: 8765,
		},
		Models: ModelsConfig{
			Default:    "gpt-5",
			Main:       "gpt-5",
			Planner:    "gpt-5-high",
			Coder:      "gpt-5",
			Reviewer:   "gpt-5",
			Summarizer: "gpt-5-mini",
		},
		Provider: ProviderConfig{
			Type: "openai_compatible",
		},
		Anthropic: AnthropicConfig{
			BaseURL:        "https://api.anthropic.com",
			APIKeyEnv:      "ANTHROPIC_API_KEY",
			TimeoutSeconds: 120.0,
		},
		OpenAICompatible: OpenAICompatibleConfig{
			Profile:              "openai",
			ProfileSchemaVersion: 1,
			BaseURL:              "https://api.openai.com/v1",
			APIKeyEnv:            "OPENAI_API_KEY",
			AuthMode:             "required",
			TimeoutSeconds:       60.0,
			ContextWindow:        32768,
			MaxOutputTokens:      8192,
			ToolCalling:          true,
			Streaming:            true,
			Tokenizer:            "chars",
			CharsPerToken:        3.5,
		},
		Pricing: map[string]ModelPriceConfig{},
	}
}

func Load() (Config, error) {
	cfg := Default()
	path, err := Path()
	if err != nil {
		return cfg, err
	}

	content, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return cfg, nil
	}
	if err != nil {
		return cfg, err
	}

	section := ""
	for _, raw := range strings.Split(string(content), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			section = strings.TrimSuffix(strings.TrimPrefix(line, "["), "]")
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.Trim(strings.TrimSpace(value), `"`)

		switch section + "." + key {
		case "ui.style":
			cfg.UI.Style = value
		case "runtime.url":
			cfg.Runtime.URL = value
		case "runtime.port":
			port, err := strconv.Atoi(value)
			if err != nil {
				return cfg, fmt.Errorf("invalid runtime.port: %w", err)
			}
			cfg.Runtime.Port = port
		case "models.default":
			cfg.Models.Default = value
		case "models.main":
			cfg.Models.Main = value
		case "models.planner":
			cfg.Models.Planner = value
		case "models.coder":
			cfg.Models.Coder = value
		case "models.reviewer":
			cfg.Models.Reviewer = value
		case "models.summarizer":
			cfg.Models.Summarizer = value
		case "provider.type":
			cfg.Provider.Type = value
		case "provider.anthropic.base_url":
			cfg.Anthropic.BaseURL = value
		case "provider.anthropic.api_key_env":
			cfg.Anthropic.APIKeyEnv = value
		case "provider.anthropic.timeout_seconds":
			timeout, err := strconv.ParseFloat(value, 64)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.anthropic.timeout_seconds: %w", err)
			}
			cfg.Anthropic.TimeoutSeconds = timeout
		case "provider.openai_compatible.base_url":
			cfg.OpenAICompatible.BaseURL = value
		case "provider.openai_compatible.api_key_env":
			cfg.OpenAICompatible.APIKeyEnv = value
		case "provider.openai_compatible.profile":
			cfg.OpenAICompatible.Profile = value
		case "provider.openai_compatible.profile_schema_version":
			version, err := strconv.Atoi(value)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.profile_schema_version: %w", err)
			}
			cfg.OpenAICompatible.ProfileSchemaVersion = version
		case "provider.openai_compatible.auth_mode":
			cfg.OpenAICompatible.AuthMode = value
		case "provider.openai_compatible.timeout_seconds":
			timeout, err := strconv.ParseFloat(value, 64)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.timeout_seconds: %w", err)
			}
			cfg.OpenAICompatible.TimeoutSeconds = timeout
		case "provider.openai_compatible.context_window":
			contextWindow, err := strconv.Atoi(value)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.context_window: %w", err)
			}
			cfg.OpenAICompatible.ContextWindow = contextWindow
		case "provider.openai_compatible.max_output_tokens":
			maxOutput, err := strconv.Atoi(value)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.max_output_tokens: %w", err)
			}
			cfg.OpenAICompatible.MaxOutputTokens = maxOutput
		case "provider.openai_compatible.tool_calling":
			enabled, err := strconv.ParseBool(value)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.tool_calling: %w", err)
			}
			cfg.OpenAICompatible.ToolCalling = enabled
		case "provider.openai_compatible.streaming":
			enabled, err := strconv.ParseBool(value)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.streaming: %w", err)
			}
			cfg.OpenAICompatible.Streaming = enabled
		case "provider.openai_compatible.tokenizer":
			cfg.OpenAICompatible.Tokenizer = value
		case "provider.openai_compatible.chars_per_token":
			ratio, err := strconv.ParseFloat(value, 64)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.chars_per_token: %w", err)
			}
			cfg.OpenAICompatible.CharsPerToken = ratio
		default:
			if strings.HasPrefix(section, "pricing.") {
				if err := setPricingValue(cfg.Pricing, section, key, value); err != nil {
					return cfg, err
				}
			}
		}
	}

	applyEnvOverrides(&cfg)
	return cfg, nil
}

func applyEnvOverrides(cfg *Config) {
	if envURL := os.Getenv("AICODE_RUNTIME_URL"); envURL != "" {
		cfg.Runtime.URL = envURL
	}
	if model := os.Getenv("AICODE_MODEL_DEFAULT"); model != "" {
		cfg.Models.Default = model
	}
	if model := os.Getenv("AICODE_MODEL_MAIN"); model != "" {
		cfg.Models.Main = model
	}
	if model := os.Getenv("AICODE_MODEL_PLANNER"); model != "" {
		cfg.Models.Planner = model
	}
	if model := os.Getenv("AICODE_MODEL_CODER"); model != "" {
		cfg.Models.Coder = model
	}
	if model := os.Getenv("AICODE_MODEL_REVIEWER"); model != "" {
		cfg.Models.Reviewer = model
	}
	if model := os.Getenv("AICODE_MODEL_SUMMARIZER"); model != "" {
		cfg.Models.Summarizer = model
	}
	if providerType := os.Getenv("AICODE_PROVIDER_TYPE"); providerType != "" {
		cfg.Provider.Type = providerType
	}
	if baseURL := os.Getenv("AICODE_ANTHROPIC_BASE_URL"); baseURL != "" {
		cfg.Anthropic.BaseURL = baseURL
	}
	if apiKeyEnv := os.Getenv("AICODE_ANTHROPIC_API_KEY_ENV"); apiKeyEnv != "" {
		cfg.Anthropic.APIKeyEnv = apiKeyEnv
	}
	if timeout := os.Getenv("AICODE_ANTHROPIC_TIMEOUT_SECONDS"); timeout != "" {
		parsed, err := strconv.ParseFloat(timeout, 64)
		if err == nil {
			cfg.Anthropic.TimeoutSeconds = parsed
		}
	}
	if baseURL := os.Getenv("AICODE_OPENAI_BASE_URL"); baseURL != "" {
		cfg.OpenAICompatible.BaseURL = baseURL
	}
	if profile := os.Getenv("AICODE_OPENAI_PROFILE"); profile != "" {
		cfg.OpenAICompatible.Profile = profile
	}
	if version := os.Getenv("AICODE_OPENAI_PROFILE_SCHEMA_VERSION"); version != "" {
		if parsed, err := strconv.Atoi(version); err == nil {
			cfg.OpenAICompatible.ProfileSchemaVersion = parsed
		}
	}
	if apiKeyEnv := os.Getenv("AICODE_OPENAI_API_KEY_ENV"); apiKeyEnv != "" {
		cfg.OpenAICompatible.APIKeyEnv = apiKeyEnv
	}
	if authMode := os.Getenv("AICODE_OPENAI_AUTH_MODE"); authMode != "" {
		cfg.OpenAICompatible.AuthMode = authMode
	}
	if timeout := os.Getenv("AICODE_OPENAI_TIMEOUT_SECONDS"); timeout != "" {
		parsed, err := strconv.ParseFloat(timeout, 64)
		if err == nil {
			cfg.OpenAICompatible.TimeoutSeconds = parsed
		}
	}
	if contextWindow := os.Getenv("AICODE_OPENAI_CONTEXT_WINDOW"); contextWindow != "" {
		if parsed, err := strconv.Atoi(contextWindow); err == nil {
			cfg.OpenAICompatible.ContextWindow = parsed
		}
	}
	if maxOutput := os.Getenv("AICODE_OPENAI_MAX_OUTPUT_TOKENS"); maxOutput != "" {
		if parsed, err := strconv.Atoi(maxOutput); err == nil {
			cfg.OpenAICompatible.MaxOutputTokens = parsed
		}
	}
	if toolCalling := os.Getenv("AICODE_OPENAI_TOOL_CALLING"); toolCalling != "" {
		if parsed, err := strconv.ParseBool(toolCalling); err == nil {
			cfg.OpenAICompatible.ToolCalling = parsed
		}
	}
	if streaming := os.Getenv("AICODE_OPENAI_STREAMING"); streaming != "" {
		if parsed, err := strconv.ParseBool(streaming); err == nil {
			cfg.OpenAICompatible.Streaming = parsed
		}
	}
	if tokenizer := os.Getenv("AICODE_OPENAI_TOKENIZER"); tokenizer != "" {
		cfg.OpenAICompatible.Tokenizer = tokenizer
	}
	if charsPerToken := os.Getenv("AICODE_OPENAI_CHARS_PER_TOKEN"); charsPerToken != "" {
		if parsed, err := strconv.ParseFloat(charsPerToken, 64); err == nil {
			cfg.OpenAICompatible.CharsPerToken = parsed
		}
	}
	if rawPrices := os.Getenv("AICODE_MODEL_PRICES_JSON"); rawPrices != "" {
		cfg.Pricing = parsePricingJSON(rawPrices)
	}
}

func (cfg Config) RuntimeEnv() []string {
	env := filteredRuntimeBaseEnv(os.Environ())
	runtime := []string{
		"AICODE_MODEL_MAIN=" + cfg.Models.Main,
		"AICODE_MODEL_REVIEWER=" + cfg.Models.Reviewer,
		"AICODE_MODEL_SUMMARIZER=" + cfg.Models.Summarizer,
		"AICODE_PROVIDER_TYPE=" + cfg.Provider.Type,
		"AICODE_ANTHROPIC_BASE_URL=" + cfg.Anthropic.BaseURL,
		"AICODE_ANTHROPIC_API_KEY_ENV=" + cfg.Anthropic.APIKeyEnv,
		"AICODE_ANTHROPIC_TIMEOUT_SECONDS=" + strconv.FormatFloat(cfg.Anthropic.TimeoutSeconds, 'f', -1, 64),
		"AICODE_OPENAI_PROFILE=" + cfg.OpenAICompatible.Profile,
		"AICODE_OPENAI_PROFILE_SCHEMA_VERSION=" + strconv.Itoa(cfg.OpenAICompatible.ProfileSchemaVersion),
		"AICODE_OPENAI_BASE_URL=" + cfg.OpenAICompatible.BaseURL,
		"AICODE_OPENAI_API_KEY_ENV=" + cfg.OpenAICompatible.APIKeyEnv,
		"AICODE_OPENAI_AUTH_MODE=" + cfg.OpenAICompatible.AuthMode,
		"AICODE_OPENAI_TIMEOUT_SECONDS=" + strconv.FormatFloat(cfg.OpenAICompatible.TimeoutSeconds, 'f', -1, 64),
		"AICODE_OPENAI_CONTEXT_WINDOW=" + strconv.Itoa(cfg.OpenAICompatible.ContextWindow),
		"AICODE_OPENAI_MAX_OUTPUT_TOKENS=" + strconv.Itoa(cfg.OpenAICompatible.MaxOutputTokens),
		"AICODE_OPENAI_TOOL_CALLING=" + strconv.FormatBool(cfg.OpenAICompatible.ToolCalling),
		"AICODE_OPENAI_STREAMING=" + strconv.FormatBool(cfg.OpenAICompatible.Streaming),
		"AICODE_OPENAI_TOKENIZER=" + cfg.OpenAICompatible.Tokenizer,
		"AICODE_OPENAI_CHARS_PER_TOKEN=" + strconv.FormatFloat(cfg.OpenAICompatible.CharsPerToken, 'f', -1, 64),
	}
	if len(cfg.Pricing) > 0 {
		if encoded, err := json.Marshal(cfg.Pricing); err == nil {
			runtime = append(runtime, "AICODE_MODEL_PRICES_JSON="+string(encoded))
		}
	}
	return append(env, runtime...)
}

func filteredRuntimeBaseEnv(env []string) []string {
	managed := map[string]bool{
		"AICODE_MODEL_DEFAULT":                 true,
		"AICODE_MODEL_MAIN":                    true,
		"AICODE_MODEL_PLANNER":                 true,
		"AICODE_MODEL_CODER":                   true,
		"AICODE_MODEL_REVIEWER":                true,
		"AICODE_MODEL_SUMMARIZER":              true,
		"AICODE_PROVIDER_TYPE":                 true,
		"AICODE_ANTHROPIC_BASE_URL":            true,
		"AICODE_ANTHROPIC_API_KEY_ENV":         true,
		"AICODE_ANTHROPIC_TIMEOUT_SECONDS":     true,
		"AICODE_OPENAI_PROFILE":                true,
		"AICODE_OPENAI_PROFILE_SCHEMA_VERSION": true,
		"AICODE_OPENAI_BASE_URL":               true,
		"AICODE_OPENAI_API_KEY_ENV":            true,
		"AICODE_OPENAI_AUTH_MODE":              true,
		"AICODE_OPENAI_TIMEOUT_SECONDS":        true,
		"AICODE_OPENAI_CONTEXT_WINDOW":         true,
		"AICODE_OPENAI_MAX_OUTPUT_TOKENS":      true,
		"AICODE_OPENAI_TOOL_CALLING":           true,
		"AICODE_OPENAI_STREAMING":              true,
		"AICODE_OPENAI_TOKENIZER":              true,
		"AICODE_OPENAI_CHARS_PER_TOKEN":        true,
		"AICODE_MODEL_PRICES_JSON":             true,
	}
	filtered := make([]string, 0, len(env))
	for _, item := range env {
		key, _, ok := strings.Cut(item, "=")
		if !ok || managed[key] {
			continue
		}
		filtered = append(filtered, item)
	}
	return filtered
}

func (cfg Config) Entries() []Entry {
	entries := []Entry{
		{"ui.style", cfg.UI.Style},
		{"runtime.url", cfg.Runtime.URL},
		{"runtime.port", strconv.Itoa(cfg.Runtime.Port)},
		{"models.main", cfg.Models.Main},
		{"models.reviewer", cfg.Models.Reviewer},
		{"models.summarizer", cfg.Models.Summarizer},
		{"provider.type", cfg.Provider.Type},
		{"provider.anthropic.base_url", cfg.Anthropic.BaseURL},
		{"provider.anthropic.api_key_env", cfg.Anthropic.APIKeyEnv},
		{"provider.anthropic.timeout_seconds", formatFloat(cfg.Anthropic.TimeoutSeconds)},
		{"provider.openai_compatible.profile", cfg.OpenAICompatible.Profile},
		{"provider.openai_compatible.profile_schema_version", strconv.Itoa(cfg.OpenAICompatible.ProfileSchemaVersion)},
		{"provider.openai_compatible.base_url", cfg.OpenAICompatible.BaseURL},
		{"provider.openai_compatible.api_key_env", cfg.OpenAICompatible.APIKeyEnv},
		{"provider.openai_compatible.auth_mode", cfg.OpenAICompatible.AuthMode},
		{"provider.openai_compatible.timeout_seconds", formatFloat(cfg.OpenAICompatible.TimeoutSeconds)},
		{"provider.openai_compatible.context_window", strconv.Itoa(cfg.OpenAICompatible.ContextWindow)},
		{"provider.openai_compatible.max_output_tokens", strconv.Itoa(cfg.OpenAICompatible.MaxOutputTokens)},
		{"provider.openai_compatible.tool_calling", strconv.FormatBool(cfg.OpenAICompatible.ToolCalling)},
		{"provider.openai_compatible.streaming", strconv.FormatBool(cfg.OpenAICompatible.Streaming)},
		{"provider.openai_compatible.tokenizer", cfg.OpenAICompatible.Tokenizer},
		{"provider.openai_compatible.chars_per_token", formatFloat(cfg.OpenAICompatible.CharsPerToken)},
	}
	for _, key := range sortedPricingKeys(cfg.Pricing) {
		price := cfg.Pricing[key]
		configKey := pricingConfigKeyPrefix(key)
		entries = append(entries,
			Entry{configKey + ".input_per_1m", formatFloat(price.InputPer1M)},
			Entry{configKey + ".output_per_1m", formatFloat(price.OutputPer1M)},
		)
	}
	return entries
}

func (cfg Config) GetValue(key string) (string, bool) {
	for _, entry := range cfg.Entries() {
		if entry.Key == key {
			return entry.Value, true
		}
	}
	return "", false
}

func KeyDocs() []KeyDoc {
	defaults := Default()
	return []KeyDoc{
		{
			Key:         "ui.style",
			Default:     defaults.UI.Style,
			Env:         "",
			Description: "CLI output style; the current default is codex.",
		},
		{
			Key:         "runtime.url",
			Default:     defaults.Runtime.URL,
			Env:         "AICODE_RUNTIME_URL",
			Description: "URL used by the CLI to connect to the Runtime daemon.",
		},
		{
			Key:         "runtime.port",
			Default:     strconv.Itoa(defaults.Runtime.Port),
			Env:         "",
			Description: "Port used when the CLI starts the Runtime daemon.",
		},
		{
			Key:         "models.main",
			Default:     defaults.Models.Main,
			Env:         "AICODE_MODEL_MAIN",
			Description: "Primary model for the v2 agent loop.",
		},
		{
			Key:         "models.reviewer",
			Default:     defaults.Models.Reviewer,
			Env:         "AICODE_MODEL_REVIEWER",
			Description: "Model used to summarize review-mode results.",
		},
		{
			Key:         "models.summarizer",
			Default:     defaults.Models.Summarizer,
			Env:         "AICODE_MODEL_SUMMARIZER",
			Description: "Model used for chat, diff, and test summaries.",
		},
		{
			Key:         "provider.type",
			Default:     defaults.Provider.Type,
			Env:         "AICODE_PROVIDER_TYPE",
			Description: "Provider type for the primary model: openai_compatible or anthropic.",
		},
		{
			Key:         "provider.anthropic.base_url",
			Default:     defaults.Anthropic.BaseURL,
			Env:         "AICODE_ANTHROPIC_BASE_URL",
			Description: "API base URL for the Anthropic provider.",
		},
		{
			Key:         "provider.anthropic.api_key_env",
			Default:     defaults.Anthropic.APIKeyEnv,
			Env:         "AICODE_ANTHROPIC_API_KEY_ENV",
			Description: "Environment variable from which Runtime reads the Anthropic API key.",
		},
		{
			Key:         "provider.anthropic.timeout_seconds",
			Default:     formatFloat(defaults.Anthropic.TimeoutSeconds),
			Env:         "AICODE_ANTHROPIC_TIMEOUT_SECONDS",
			Description: "Anthropic provider request timeout in seconds.",
		},
		{
			Key:         "provider.openai_compatible.profile",
			Default:     defaults.OpenAICompatible.Profile,
			Env:         "AICODE_OPENAI_PROFILE",
			Description: "Provider Profile name, such as openai, ollama, llama_cpp, or lm_studio.",
		},
		{
			Key:         "provider.openai_compatible.profile_schema_version",
			Default:     strconv.Itoa(defaults.OpenAICompatible.ProfileSchemaVersion),
			Env:         "AICODE_OPENAI_PROFILE_SCHEMA_VERSION",
			Description: "Provider Profile contract version; currently 1.",
		},
		{
			Key:         "provider.openai_compatible.base_url",
			Default:     defaults.OpenAICompatible.BaseURL,
			Env:         "AICODE_OPENAI_BASE_URL",
			Description: "API base URL for the OpenAI-compatible provider.",
		},
		{
			Key:         "provider.openai_compatible.api_key_env",
			Default:     defaults.OpenAICompatible.APIKeyEnv,
			Env:         "AICODE_OPENAI_API_KEY_ENV",
			Description: "Environment variable from which Runtime reads the provider API key.",
		},
		{
			Key:         "provider.openai_compatible.auth_mode",
			Default:     defaults.OpenAICompatible.AuthMode,
			Env:         "AICODE_OPENAI_AUTH_MODE",
			Description: "Authentication mode: required, optional, or none. Use none for a local no-auth endpoint.",
		},
		{
			Key:         "provider.openai_compatible.timeout_seconds",
			Default:     formatFloat(defaults.OpenAICompatible.TimeoutSeconds),
			Env:         "AICODE_OPENAI_TIMEOUT_SECONDS",
			Description: "OpenAI-compatible request timeout in seconds.",
		},
		{
			Key:         "provider.openai_compatible.context_window",
			Default:     strconv.Itoa(defaults.OpenAICompatible.ContextWindow),
			Env:         "AICODE_OPENAI_CONTEXT_WINDOW",
			Description: "Default context-window token count for this profile.",
		},
		{
			Key:         "provider.openai_compatible.max_output_tokens",
			Default:     strconv.Itoa(defaults.OpenAICompatible.MaxOutputTokens),
			Env:         "AICODE_OPENAI_MAX_OUTPUT_TOKENS",
			Description: "Default maximum output-token count for this profile.",
		},
		{
			Key:         "provider.openai_compatible.tool_calling",
			Default:     strconv.FormatBool(defaults.OpenAICompatible.ToolCalling),
			Env:         "AICODE_OPENAI_TOOL_CALLING",
			Description: "Whether the profile supports native OpenAI tools; false makes the Agent fail fast.",
		},
		{
			Key:         "provider.openai_compatible.streaming",
			Default:     strconv.FormatBool(defaults.OpenAICompatible.Streaming),
			Env:         "AICODE_OPENAI_STREAMING",
			Description: "Whether the profile supports SSE streaming; the Agent currently requires true.",
		},
		{
			Key:         "provider.openai_compatible.tokenizer",
			Default:     defaults.OpenAICompatible.Tokenizer,
			Env:         "AICODE_OPENAI_TOKENIZER",
			Description: "Token-budget estimation strategy; currently chars.",
		},
		{
			Key:         "provider.openai_compatible.chars_per_token",
			Default:     formatFloat(defaults.OpenAICompatible.CharsPerToken),
			Env:         "AICODE_OPENAI_CHARS_PER_TOKEN",
			Description: "Estimated characters per token for the chars tokenizer.",
		},
		{
			Key:         "pricing.<provider>.<model>.input_per_1m",
			Default:     "unset",
			Env:         "AICODE_MODEL_PRICES_JSON",
			Description: "Input-token price for local cost estimates, in USD per 1M tokens.",
		},
		{
			Key:         "pricing.<provider>.<model>.output_per_1m",
			Default:     "unset",
			Env:         "AICODE_MODEL_PRICES_JSON",
			Description: "Output-token price for local cost estimates, in USD per 1M tokens.",
		},
	}
}

func Init() (string, error) {
	path, err := Path()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", err
	}
	if _, err := os.Stat(path); err == nil {
		return path, nil
	}
	return path, os.WriteFile(path, []byte(DefaultContent()), 0o600)
}

func ReadRaw() (string, string, error) {
	path, err := Path()
	if err != nil {
		return "", "", err
	}
	content, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return DefaultContent(), path, nil
	}
	if err != nil {
		return "", path, err
	}
	return string(content), path, nil
}

func SetValue(key string, value string) (string, error) {
	section, name, err := configKeyTarget(key)
	if err != nil {
		return "", err
	}
	formatted, err := formatConfigLine(section, name, value)
	if err != nil {
		return "", err
	}

	path, err := Init()
	if err != nil {
		return "", err
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}

	lines := strings.Split(string(content), "\n")
	lines = upsertConfigLine(lines, section, name, formatted)
	return path, os.WriteFile(path, []byte(strings.Join(lines, "\n")), 0o600)
}

func UnsetValue(key string) (string, bool, error) {
	section, name, err := configKeyTarget(key)
	if err != nil {
		return "", false, err
	}

	path, err := Path()
	if err != nil {
		return "", false, err
	}
	content, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return path, false, nil
	}
	if err != nil {
		return path, false, err
	}

	lines, removed := removeConfigLine(strings.Split(string(content), "\n"), section, name)
	if !removed {
		return path, false, nil
	}
	return path, true, os.WriteFile(path, []byte(strings.Join(lines, "\n")), 0o600)
}

func configKeyTarget(key string) (string, string, error) {
	if strings.HasPrefix(key, "pricing.") {
		section, name, ok := strings.Cut(strings.TrimPrefix(key, "pricing."), ".")
		if !ok {
			return "", "", fmt.Errorf("pricing key must be pricing.<provider>.<model>.<input_per_1m|output_per_1m>")
		}
		lastDot := strings.LastIndex(name, ".")
		if lastDot < 0 {
			return "", "", fmt.Errorf("pricing key must be pricing.<provider>.<model>.<input_per_1m|output_per_1m>")
		}
		model := name[:lastDot]
		field := name[lastDot+1:]
		if strings.TrimSpace(section) == "" || strings.TrimSpace(model) == "" {
			return "", "", fmt.Errorf("pricing provider and model must not be empty")
		}
		if field != "input_per_1m" && field != "output_per_1m" {
			return "", "", fmt.Errorf("pricing supports only input_per_1m or output_per_1m")
		}
		return "pricing." + section + "." + model, field, nil
	}

	supported := map[string][2]string{
		"ui.style":                           {"ui", "style"},
		"runtime.url":                        {"runtime", "url"},
		"runtime.port":                       {"runtime", "port"},
		"models.main":                        {"models", "main"},
		"models.reviewer":                    {"models", "reviewer"},
		"models.summarizer":                  {"models", "summarizer"},
		"provider.type":                      {"provider", "type"},
		"provider.anthropic.base_url":        {"provider.anthropic", "base_url"},
		"provider.anthropic.api_key_env":     {"provider.anthropic", "api_key_env"},
		"provider.anthropic.timeout_seconds": {"provider.anthropic", "timeout_seconds"},
		"provider.openai_compatible.profile": {"provider.openai_compatible", "profile"},
		"provider.openai_compatible.profile_schema_version": {
			"provider.openai_compatible", "profile_schema_version",
		},
		"provider.openai_compatible.base_url":        {"provider.openai_compatible", "base_url"},
		"provider.openai_compatible.api_key_env":     {"provider.openai_compatible", "api_key_env"},
		"provider.openai_compatible.auth_mode":       {"provider.openai_compatible", "auth_mode"},
		"provider.openai_compatible.timeout_seconds": {"provider.openai_compatible", "timeout_seconds"},
		"provider.openai_compatible.context_window":  {"provider.openai_compatible", "context_window"},
		"provider.openai_compatible.max_output_tokens": {
			"provider.openai_compatible", "max_output_tokens",
		},
		"provider.openai_compatible.tool_calling":    {"provider.openai_compatible", "tool_calling"},
		"provider.openai_compatible.streaming":       {"provider.openai_compatible", "streaming"},
		"provider.openai_compatible.tokenizer":       {"provider.openai_compatible", "tokenizer"},
		"provider.openai_compatible.chars_per_token": {"provider.openai_compatible", "chars_per_token"},
	}
	target, ok := supported[key]
	if !ok {
		keys := make([]string, 0, len(supported))
		for key := range supported {
			keys = append(keys, key)
		}
		slices.Sort(keys)
		return "", "", fmt.Errorf("supported keys: %s", strings.Join(keys, ", "))
	}
	return target[0], target[1], nil
}

func formatConfigLine(section string, key string, value string) (string, error) {
	if (section == "runtime" && key == "port") ||
		(section == "provider.openai_compatible" &&
			(key == "profile_schema_version" || key == "context_window" || key == "max_output_tokens")) {
		if _, err := strconv.Atoi(value); err != nil {
			return "", fmt.Errorf("%s.%s must be an integer: %w", section, key, err)
		}
		return fmt.Sprintf("%s = %s", key, value), nil
	}
	if ((section == "provider.openai_compatible" || section == "provider.anthropic") && key == "timeout_seconds") ||
		(section == "provider.openai_compatible" && key == "chars_per_token") ||
		strings.HasPrefix(section, "pricing.") {
		if _, err := strconv.ParseFloat(value, 64); err != nil {
			return "", fmt.Errorf("%s.%s must be a number: %w", section, key, err)
		}
		return fmt.Sprintf("%s = %s", key, value), nil
	}
	if section == "provider.openai_compatible" && (key == "tool_calling" || key == "streaming") {
		if _, err := strconv.ParseBool(value); err != nil {
			return "", fmt.Errorf("%s.%s must be true or false: %w", section, key, err)
		}
		return fmt.Sprintf("%s = %s", key, value), nil
	}
	return fmt.Sprintf("%s = %q", key, value), nil
}

func setPricingValue(pricing map[string]ModelPriceConfig, section string, key string, value string) error {
	if key != "input_per_1m" && key != "output_per_1m" {
		return nil
	}
	price, err := strconv.ParseFloat(strings.Trim(strings.TrimSpace(value), `"`), 64)
	if err != nil {
		return fmt.Errorf("invalid %s.%s: %w", section, key, err)
	}
	parts := strings.SplitN(strings.TrimPrefix(section, "pricing."), ".", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || strings.TrimSpace(parts[1]) == "" {
		return fmt.Errorf("invalid pricing section: %s", section)
	}
	priceKey := parts[0] + "/" + parts[1]
	current := pricing[priceKey]
	if key == "input_per_1m" {
		current.InputPer1M = price
	} else {
		current.OutputPer1M = price
	}
	pricing[priceKey] = current
	return nil
}

func parsePricingJSON(raw string) map[string]ModelPriceConfig {
	var parsed map[string]ModelPriceConfig
	if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
		return map[string]ModelPriceConfig{}
	}
	if parsed == nil {
		return map[string]ModelPriceConfig{}
	}
	return parsed
}

func sortedPricingKeys(pricing map[string]ModelPriceConfig) []string {
	keys := make([]string, 0, len(pricing))
	for key := range pricing {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}

func pricingConfigKeyPrefix(priceKey string) string {
	provider, model, ok := strings.Cut(priceKey, "/")
	if !ok {
		return "pricing." + priceKey
	}
	return "pricing." + provider + "." + model
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'f', -1, 64)
}

func upsertConfigLine(lines []string, targetSection string, targetKey string, formatted string) []string {
	currentSection := ""
	sectionStart := -1
	sectionEnd := len(lines)

	for i, raw := range lines {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			if sectionStart >= 0 {
				sectionEnd = i
				break
			}
			currentSection = strings.TrimSuffix(strings.TrimPrefix(line, "["), "]")
			if currentSection == targetSection {
				sectionStart = i
			}
			continue
		}
		if sectionStart >= 0 && currentSection == targetSection {
			key, _, ok := strings.Cut(line, "=")
			if ok && strings.TrimSpace(key) == targetKey {
				lines[i] = formatted
				return lines
			}
		}
	}

	if sectionStart >= 0 {
		return insertLine(lines, sectionEnd, formatted)
	}
	if len(lines) > 0 && strings.TrimSpace(lines[len(lines)-1]) != "" {
		lines = append(lines, "")
	}
	lines = append(lines, "["+targetSection+"]", formatted)
	return lines
}

func removeConfigLine(lines []string, targetSection string, targetKey string) ([]string, bool) {
	currentSection := ""
	sectionStart := -1
	sectionEnd := len(lines)
	removed := false
	next := make([]string, 0, len(lines))

	for i, raw := range lines {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			if sectionStart >= 0 && sectionEnd == len(lines) {
				sectionEnd = i
			}
			currentSection = strings.TrimSuffix(strings.TrimPrefix(line, "["), "]")
			if currentSection == targetSection {
				sectionStart = len(next)
			}
			next = append(next, raw)
			continue
		}
		if currentSection == targetSection {
			key, _, ok := strings.Cut(line, "=")
			if ok && strings.TrimSpace(key) == targetKey {
				removed = true
				continue
			}
		}
		next = append(next, raw)
	}
	if !removed {
		return lines, false
	}

	if sectionStart >= 0 && shouldRemoveEmptySection(next, sectionStart, sectionEnd, targetSection) {
		next = removeSection(next, sectionStart)
	}
	return trimTrailingBlankLines(next), true
}

func shouldRemoveEmptySection(lines []string, sectionStart int, originalSectionEnd int, section string) bool {
	if !strings.HasPrefix(section, "pricing.") {
		return false
	}
	sectionEnd := originalSectionEnd
	if sectionEnd > len(lines) {
		sectionEnd = len(lines)
	}
	for i := sectionStart + 1; i < sectionEnd; i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.Contains(line, "=") {
			return false
		}
	}
	return true
}

func removeSection(lines []string, sectionStart int) []string {
	sectionEnd := len(lines)
	for i := sectionStart + 1; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			sectionEnd = i
			break
		}
	}
	next := append([]string{}, lines[:sectionStart]...)
	next = append(next, lines[sectionEnd:]...)
	return trimRepeatedBlankLines(next)
}

func trimRepeatedBlankLines(lines []string) []string {
	next := make([]string, 0, len(lines))
	previousBlank := false
	for _, line := range lines {
		blank := strings.TrimSpace(line) == ""
		if blank && previousBlank {
			continue
		}
		next = append(next, line)
		previousBlank = blank
	}
	return next
}

func trimTrailingBlankLines(lines []string) []string {
	for len(lines) > 0 && strings.TrimSpace(lines[len(lines)-1]) == "" {
		lines = lines[:len(lines)-1]
	}
	return append(lines, "")
}

func insertLine(lines []string, index int, line string) []string {
	if index < 0 || index > len(lines) {
		index = len(lines)
	}
	lines = append(lines, "")
	copy(lines[index+1:], lines[index:])
	lines[index] = line
	return lines
}

func Path() (string, error) {
	home, err := Home()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, "config.toml"), nil
}

func Home() (string, error) {
	if custom := os.Getenv("AICODE_HOME"); custom != "" {
		return custom, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".aicode"), nil
}

func DefaultContent() string {
	return `[ui]
style = "codex"

[runtime]
url = "http://127.0.0.1:8765"
port = 8765

[models]
main = "gpt-5"
reviewer = "gpt-5"
summarizer = "gpt-5-mini"

[provider]
type = "openai_compatible"

[provider.anthropic]
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
timeout_seconds = 120

[provider.openai_compatible]
profile = "openai"
profile_schema_version = 1
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
auth_mode = "required"
timeout_seconds = 60
context_window = 32768
max_output_tokens = 8192
tool_calling = true
streaming = true
tokenizer = "chars"
chars_per_token = 3.5

[permissions]
file_write = "ask"
shell = "allow_low_risk"
network = "ask"
delete_file = "deny"
git_destructive = "deny"

[usage]
track_tokens = true
track_cost = true
storage = "local"
`
}
