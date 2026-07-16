package config

import (
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
	OpenAICompatible OpenAICompatibleConfig
}

type UIConfig struct {
	Language string
	Style    string
}

type RuntimeConfig struct {
	URL  string
	Port int
}

type ModelsConfig struct {
	Default    string
	Planner    string
	Coder      string
	Reviewer   string
	Summarizer string
}

type OpenAICompatibleConfig struct {
	BaseURL        string
	APIKeyEnv      string
	TimeoutSeconds float64
}

func Default() Config {
	return Config{
		UI: UIConfig{
			Language: "zh-CN",
			Style:    "codex",
		},
		Runtime: RuntimeConfig{
			URL:  "http://127.0.0.1:8765",
			Port: 8765,
		},
		Models: ModelsConfig{
			Default:    "gpt-5",
			Planner:    "gpt-5-high",
			Coder:      "gpt-5",
			Reviewer:   "gpt-5",
			Summarizer: "gpt-5-mini",
		},
		OpenAICompatible: OpenAICompatibleConfig{
			BaseURL:        "https://api.openai.com/v1",
			APIKeyEnv:      "OPENAI_API_KEY",
			TimeoutSeconds: 60.0,
		},
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
		case "ui.language":
			cfg.UI.Language = value
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
		case "models.planner":
			cfg.Models.Planner = value
		case "models.coder":
			cfg.Models.Coder = value
		case "models.reviewer":
			cfg.Models.Reviewer = value
		case "models.summarizer":
			cfg.Models.Summarizer = value
		case "provider.openai_compatible.base_url":
			cfg.OpenAICompatible.BaseURL = value
		case "provider.openai_compatible.api_key_env":
			cfg.OpenAICompatible.APIKeyEnv = value
		case "provider.openai_compatible.timeout_seconds":
			timeout, err := strconv.ParseFloat(value, 64)
			if err != nil {
				return cfg, fmt.Errorf("invalid provider.openai_compatible.timeout_seconds: %w", err)
			}
			cfg.OpenAICompatible.TimeoutSeconds = timeout
		}
	}

	applyEnvOverrides(&cfg)
	return cfg, nil
}

func applyEnvOverrides(cfg *Config) {
	if envURL := os.Getenv("AICODE_RUNTIME_URL"); envURL != "" {
		cfg.Runtime.URL = envURL
	}
	if envLanguage := os.Getenv("AICODE_DEFAULT_LANGUAGE"); envLanguage != "" {
		cfg.UI.Language = envLanguage
	}
	if model := os.Getenv("AICODE_MODEL_DEFAULT"); model != "" {
		cfg.Models.Default = model
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
	if baseURL := os.Getenv("AICODE_OPENAI_BASE_URL"); baseURL != "" {
		cfg.OpenAICompatible.BaseURL = baseURL
	}
	if apiKeyEnv := os.Getenv("AICODE_OPENAI_API_KEY_ENV"); apiKeyEnv != "" {
		cfg.OpenAICompatible.APIKeyEnv = apiKeyEnv
	}
	if timeout := os.Getenv("AICODE_OPENAI_TIMEOUT_SECONDS"); timeout != "" {
		parsed, err := strconv.ParseFloat(timeout, 64)
		if err == nil {
			cfg.OpenAICompatible.TimeoutSeconds = parsed
		}
	}
}

func (cfg Config) RuntimeEnv() []string {
	env := os.Environ()
	runtime := []string{
		"AICODE_DEFAULT_LANGUAGE=" + cfg.UI.Language,
		"AICODE_MODEL_DEFAULT=" + cfg.Models.Default,
		"AICODE_MODEL_PLANNER=" + cfg.Models.Planner,
		"AICODE_MODEL_CODER=" + cfg.Models.Coder,
		"AICODE_MODEL_REVIEWER=" + cfg.Models.Reviewer,
		"AICODE_MODEL_SUMMARIZER=" + cfg.Models.Summarizer,
		"AICODE_OPENAI_BASE_URL=" + cfg.OpenAICompatible.BaseURL,
		"AICODE_OPENAI_API_KEY_ENV=" + cfg.OpenAICompatible.APIKeyEnv,
		"AICODE_OPENAI_TIMEOUT_SECONDS=" + strconv.FormatFloat(cfg.OpenAICompatible.TimeoutSeconds, 'f', -1, 64),
	}
	return append(env, runtime...)
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

func configKeyTarget(key string) (string, string, error) {
	supported := map[string][2]string{
		"ui.language":                                {"ui", "language"},
		"models.default":                             {"models", "default"},
		"models.planner":                             {"models", "planner"},
		"models.coder":                               {"models", "coder"},
		"models.reviewer":                            {"models", "reviewer"},
		"models.summarizer":                          {"models", "summarizer"},
		"provider.openai_compatible.base_url":        {"provider.openai_compatible", "base_url"},
		"provider.openai_compatible.api_key_env":     {"provider.openai_compatible", "api_key_env"},
		"provider.openai_compatible.timeout_seconds": {"provider.openai_compatible", "timeout_seconds"},
	}
	target, ok := supported[key]
	if !ok {
		keys := make([]string, 0, len(supported))
		for key := range supported {
			keys = append(keys, key)
		}
		slices.Sort(keys)
		return "", "", fmt.Errorf("暂只支持设置: %s", strings.Join(keys, ", "))
	}
	return target[0], target[1], nil
}

func formatConfigLine(section string, key string, value string) (string, error) {
	if section == "provider.openai_compatible" && key == "timeout_seconds" {
		if _, err := strconv.ParseFloat(value, 64); err != nil {
			return "", fmt.Errorf("provider.openai_compatible.timeout_seconds 必须是数字: %w", err)
		}
		return fmt.Sprintf("%s = %s", key, value), nil
	}
	return fmt.Sprintf("%s = %q", key, value), nil
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
language = "zh-CN"
style = "codex"

[runtime]
url = "http://127.0.0.1:8765"
port = 8765

[models]
default = "gpt-5"
planner = "gpt-5-high"
coder = "gpt-5"
reviewer = "gpt-5"
summarizer = "gpt-5-mini"

[provider.openai_compatible]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 60

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
