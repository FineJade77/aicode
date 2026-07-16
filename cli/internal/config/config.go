package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	UI      UIConfig
	Runtime RuntimeConfig
}

type UIConfig struct {
	Language string
	Style    string
}

type RuntimeConfig struct {
	URL  string
	Port int
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
		}
	}

	if envURL := os.Getenv("AICODE_RUNTIME_URL"); envURL != "" {
		cfg.Runtime.URL = envURL
	}
	return cfg, nil
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
	if key != "ui.language" {
		return "", fmt.Errorf("暂只支持设置 ui.language")
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
	section := ""
	updated := false
	for i, raw := range lines {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "[") && strings.HasSuffix(line, "]") {
			section = strings.TrimSuffix(strings.TrimPrefix(line, "["), "]")
			continue
		}
		if section == "ui" && strings.HasPrefix(line, "language") {
			lines[i] = fmt.Sprintf("language = %q", value)
			updated = true
			break
		}
	}
	if !updated {
		lines = append(lines, "[ui]", fmt.Sprintf("language = %q", value))
	}
	return path, os.WriteFile(path, []byte(strings.Join(lines, "\n")), 0o600)
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
planner = "gpt-5-high"
coder = "gpt-5"
reviewer = "gpt-5"
summarizer = "gpt-5-mini"

[provider.openai_compatible]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

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
