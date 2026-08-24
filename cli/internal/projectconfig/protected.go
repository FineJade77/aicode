package projectconfig

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
)

func DefaultProtectedPaths() []string {
	return append(MandatoryProtectedPaths(), "secrets/**", "infra/prod/**")
}

func MandatoryProtectedPaths() []string {
	return []string{
		".env", ".env.*", "**/.env", "**/.env.*",
		".ssh/**", "**/.ssh/**", ".gnupg/**", "**/.gnupg/**",
		".aws/**", "**/.aws/**", ".azure/**", "**/.azure/**",
		".kube/**", "**/.kube/**", ".config/gcloud/**", "**/.config/gcloud/**",
		".config/gh/**", "**/.config/gh/**", ".docker/**", "**/.docker/**",
		".git/config", "**/.git/config", ".git-credentials", "**/.git-credentials",
		".netrc", "**/.netrc", ".npmrc", "**/.npmrc", ".pypirc", "**/.pypirc",
		"*.pem", "**/*.pem", "*.key", "**/*.key",
	}
}

func ListProtectedPaths(workspacePath string) (string, []string, bool, error) {
	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, nil, false, err
	}
	values, explicit := effectiveProtectedPaths(raw)
	return path, values, explicit, nil
}

func AddProtectedPath(workspacePath string, pattern string) (string, []string, error) {
	pattern = strings.TrimSpace(pattern)
	if pattern == "" {
		return "", nil, errors.New("protected path must not be empty")
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, nil, err
	}

	values, _ := effectiveProtectedPaths(raw)
	values = appendUnique(values, pattern)
	sort.Strings(values)
	raw["protectedPaths"] = stringValuesForJSON(values)
	return path, values, writeProjectConfig(path, raw)
}

func RemoveProtectedPath(workspacePath string, pattern string) (string, bool, []string, error) {
	pattern = strings.TrimSpace(pattern)
	if pattern == "" {
		return "", false, nil, errors.New("protected path must not be empty")
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, false, nil, err
	}

	values, _ := effectiveProtectedPaths(raw)
	if containsString(MandatoryProtectedPaths(), pattern) {
		return path, false, values, fmt.Errorf("mandatory sensitive protected path cannot be removed: %s", pattern)
	}
	removed := containsString(values, pattern)
	values = removeValue(values, pattern)
	sort.Strings(values)
	raw["protectedPaths"] = stringValuesForJSON(values)
	return path, removed, values, writeProjectConfig(path, raw)
}

func ResetProtectedPaths(workspacePath string) (string, []string, error) {
	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, nil, err
	}
	delete(raw, "protectedPaths")
	return path, DefaultProtectedPaths(), writeProjectConfig(path, raw)
}

func effectiveProtectedPaths(raw map[string]any) ([]string, bool) {
	if _, ok := raw["protectedPaths"]; !ok {
		values := DefaultProtectedPaths()
		sort.Strings(values)
		return values, false
	}
	values := stringList(raw["protectedPaths"])
	for _, mandatory := range MandatoryProtectedPaths() {
		values = appendUnique(values, mandatory)
	}
	sort.Strings(values)
	return values, true
}

func stringValuesForJSON(values []string) []any {
	raw := make([]any, 0, len(values))
	for _, value := range values {
		raw = append(raw, value)
	}
	return raw
}

func containsString(values []string, value string) bool {
	for _, existing := range values {
		if existing == value {
			return true
		}
	}
	return false
}
