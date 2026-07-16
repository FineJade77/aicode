package workspace

import (
	"os"
	"path/filepath"
)

type Workspace struct {
	Path string
}

func Detect() (Workspace, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return Workspace{}, err
	}

	root := findGitRoot(cwd)
	if root == "" {
		root = cwd
	}
	return Workspace{Path: root}, nil
}

func findGitRoot(start string) string {
	current := start
	for {
		if _, err := os.Stat(filepath.Join(current, ".git")); err == nil {
			return current
		}
		parent := filepath.Dir(current)
		if parent == current {
			return ""
		}
		current = parent
	}
}
