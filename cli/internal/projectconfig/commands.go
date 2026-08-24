package projectconfig

import (
	"errors"
	"path/filepath"
	"strings"
)

func GetTestCommand(workspacePath string) (string, string, bool, error) {
	return GetCommand(workspacePath, "test")
}

func GetCommand(workspacePath string, name string) (string, string, bool, error) {
	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, "", false, err
	}
	commands := objectValue(raw["commands"])
	value, ok := commands[name].(string)
	value = strings.TrimSpace(value)
	if !ok || value == "" {
		return path, "", false, nil
	}
	return path, value, true, nil
}

func SetTestCommand(workspacePath string, command string) (string, string, error) {
	command = strings.TrimSpace(command)
	if command == "" {
		return "", "", errors.New("test command must not be empty")
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, command, err
	}

	commands := objectValue(raw["commands"])
	commands["test"] = command
	raw["commands"] = commands
	if err := writeProjectConfig(path, raw); err != nil {
		return path, command, err
	}
	return path, command, nil
}

func UnsetTestCommand(workspacePath string) (string, bool, error) {
	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, false, err
	}

	commands := objectValue(raw["commands"])
	_, removed := commands["test"]
	delete(commands, "test")
	if len(commands) == 0 {
		delete(raw, "commands")
	} else {
		raw["commands"] = commands
	}
	if err := writeProjectConfig(path, raw); err != nil {
		return path, removed, err
	}
	return path, removed, nil
}
