package projectconfig

import (
	"errors"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
)

type WorkspaceEntry struct {
	Name string
	Path string
	Mode string
}

func ListWorkspaces(workspacePath string) (string, []WorkspaceEntry, error) {
	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, nil, err
	}
	return path, workspaceEntries(raw["workspaces"]), nil
}

func SetWorkspace(workspacePath string, name string, targetPath string) (string, []WorkspaceEntry, error) {
	entry, err := newWorkspaceEntry(name, targetPath)
	if err != nil {
		return "", nil, err
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, nil, err
	}

	entries := workspaceEntries(raw["workspaces"])
	replaced := false
	for index, existing := range entries {
		if existing.Name == entry.Name {
			entries[index] = entry
			replaced = true
			break
		}
	}
	if !replaced {
		entries = append(entries, entry)
	}
	sortWorkspaceEntries(entries)
	raw["workspaces"] = workspaceEntriesForJSON(entries)

	return path, entries, writeProjectConfig(path, raw)
}

func RemoveWorkspace(workspacePath string, name string) (string, bool, []WorkspaceEntry, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return "", false, nil, errors.New("workspace name must not be empty")
	}

	path := filepath.Join(workspacePath, ".aicode", "config.json")
	raw, err := readProjectConfig(path)
	if err != nil {
		return path, false, nil, err
	}

	entries := workspaceEntries(raw["workspaces"])
	next := make([]WorkspaceEntry, 0, len(entries))
	removed := false
	for _, entry := range entries {
		if entry.Name == name {
			removed = true
			continue
		}
		next = append(next, entry)
	}
	sortWorkspaceEntries(next)
	raw["workspaces"] = workspaceEntriesForJSON(next)

	return path, removed, next, writeProjectConfig(path, raw)
}

func newWorkspaceEntry(name string, targetPath string) (WorkspaceEntry, error) {
	name = strings.TrimSpace(name)
	targetPath = strings.TrimSpace(targetPath)
	if name == "" {
		return WorkspaceEntry{}, errors.New("workspace name must not be empty")
	}
	if strings.ContainsAny(name, ":/\\ \t\r\n") {
		return WorkspaceEntry{}, fmt.Errorf("workspace name must be a short name without whitespace, colons, or path separators: %s", name)
	}
	if targetPath == "" {
		return WorkspaceEntry{}, errors.New("workspace path must not be empty")
	}
	return WorkspaceEntry{Name: name, Path: targetPath, Mode: "read_only"}, nil
}

func workspaceEntries(value any) []WorkspaceEntry {
	raw, ok := value.([]any)
	if !ok {
		return []WorkspaceEntry{}
	}

	entries := make([]WorkspaceEntry, 0, len(raw))
	seen := map[string]bool{}
	for _, item := range raw {
		entry, ok := workspaceEntry(item)
		if !ok || seen[entry.Name] {
			continue
		}
		seen[entry.Name] = true
		entries = append(entries, entry)
	}
	sortWorkspaceEntries(entries)
	return entries
}

func workspaceEntry(value any) (WorkspaceEntry, bool) {
	raw, ok := value.(map[string]any)
	if !ok {
		return WorkspaceEntry{}, false
	}
	name, ok := raw["name"].(string)
	if !ok {
		return WorkspaceEntry{}, false
	}
	targetPath, ok := raw["path"].(string)
	if !ok {
		return WorkspaceEntry{}, false
	}
	mode, ok := raw["mode"].(string)
	if !ok || mode == "" {
		mode = "read_only"
	}
	name = strings.TrimSpace(name)
	targetPath = strings.TrimSpace(targetPath)
	if name == "" || targetPath == "" {
		return WorkspaceEntry{}, false
	}
	return WorkspaceEntry{Name: name, Path: targetPath, Mode: mode}, true
}

func workspaceEntriesForJSON(entries []WorkspaceEntry) []any {
	raw := make([]any, 0, len(entries))
	for _, entry := range entries {
		raw = append(raw, map[string]any{
			"name": entry.Name,
			"path": entry.Path,
			"mode": entry.Mode,
		})
	}
	return raw
}

func sortWorkspaceEntries(entries []WorkspaceEntry) {
	sort.Slice(entries, func(i int, j int) bool {
		return entries[i].Name < entries[j].Name
	})
}
