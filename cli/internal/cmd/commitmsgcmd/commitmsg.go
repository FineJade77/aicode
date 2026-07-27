// Package commitmsgcmd implements `aicode commit-message`.
package commitmsgcmd

import (
	"bytes"
	"fmt"
	"os/exec"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/cmd/agentrun"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

const maxCommitDiffChars = 60_000

type commitDiffContext struct {
	Source    string
	Status    string
	Stat      string
	Diff      string
	Truncated bool
}

func Run(cfg config.Config) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	diffContext, err := collectCommitDiffContext(root.Path)
	if err != nil {
		return err
	}
	return agentrun.Run(cfg, "commit_message", commitMessagePrompt(diffContext))
}

func collectCommitDiffContext(workspacePath string) (commitDiffContext, error) {
	status, err := gitOutput(workspacePath, "status", "--short")
	if err != nil {
		return commitDiffContext{}, err
	}

	stagedDiff, err := gitOutput(workspacePath, "diff", "--cached", "--no-ext-diff")
	if err != nil {
		return commitDiffContext{}, err
	}
	source := "staged"
	diff := stagedDiff
	statArgs := []string{"diff", "--cached", "--stat", "--no-ext-diff"}
	if strings.TrimSpace(diff) == "" {
		workingDiff, err := gitOutput(workspacePath, "diff", "--no-ext-diff")
		if err != nil {
			return commitDiffContext{}, err
		}
		source = "working tree"
		diff = workingDiff
		statArgs = []string{"diff", "--stat", "--no-ext-diff"}
	}
	if strings.TrimSpace(diff) == "" {
		return commitDiffContext{}, fmt.Errorf("no staged or working-tree tracked diff is available for a commit message")
	}
	stat, err := gitOutput(workspacePath, statArgs...)
	if err != nil {
		return commitDiffContext{}, err
	}
	truncatedDiff, truncated := truncateCommitDiff(diff, maxCommitDiffChars)
	return commitDiffContext{
		Source:    source,
		Status:    strings.TrimSpace(status),
		Stat:      strings.TrimSpace(stat),
		Diff:      truncatedDiff,
		Truncated: truncated,
	}, nil
}

func gitOutput(workspacePath string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = workspacePath
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	output, err := cmd.Output()
	if err != nil {
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = err.Error()
		}
		return "", fmt.Errorf("git %s failed: %s", strings.Join(args, " "), detail)
	}
	return string(output), nil
}

func truncateCommitDiff(diff string, limit int) (string, bool) {
	runes := []rune(diff)
	if len(runes) <= limit {
		return diff, false
	}
	return string(runes[:limit]) + "\n\n[diff truncated: omitted remaining content]\n", true
}

func commitMessagePrompt(diffContext commitDiffContext) string {
	return fmt.Sprintf(`Generate a git commit message from the following diff.

Requirements:
- Output only the commit message, with no explanation or surrounding Markdown.
- Prefer Conventional Commits style: feat/fix/docs/refactor/test/chore.
- Keep the first line at 72 characters or fewer.
- Add a blank line and 2-4 concise bullet points only when useful.
- Base the message only on the supplied status/diff.

Diff source: %s
Diff truncated: %v

Git status:
%s

Diff stat:
%s

Diff:
%s`, diffContext.Source, diffContext.Truncated, emptyFallback(diffContext.Status, "(empty)"), emptyFallback(diffContext.Stat, "(empty)"), diffContext.Diff)
}

func emptyFallback(value string, fallback string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return fallback
	}
	return value
}
