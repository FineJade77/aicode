package commitmsgcmd

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestCollectCommitDiffContextPrefersStagedDiff(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not available")
	}
	root := t.TempDir()
	runGitForTest(t, root, "init")
	if err := os.WriteFile(filepath.Join(root, "staged.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "unstaged.txt"), []byte("ignored for commit message\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitForTest(t, root, "add", "staged.txt")

	context, err := collectCommitDiffContext(root)
	if err != nil {
		t.Fatal(err)
	}

	if context.Source != "staged" {
		t.Fatalf("source = %q", context.Source)
	}
	if !strings.Contains(context.Status, "A  staged.txt") {
		t.Fatalf("status = %q", context.Status)
	}
	if !strings.Contains(context.Diff, "+hello") {
		t.Fatalf("diff = %q", context.Diff)
	}
	if strings.Contains(context.Diff, "ignored for commit message") {
		t.Fatalf("staged diff included unstaged file: %q", context.Diff)
	}
}

func TestCommitMessagePromptIsOutputOnly(t *testing.T) {
	prompt := commitMessagePrompt(commitDiffContext{
		Source: "working tree",
		Status: " M cli/main.go",
		Stat:   "cli/main.go | 2 ++",
		Diff:   "+case \"commit-message\":",
	})

	for _, want := range []string{
		"Output only the commit message",
		"Conventional Commits",
		"Diff source: working tree",
		"+case \"commit-message\":",
	} {
		if !strings.Contains(prompt, want) {
			t.Fatalf("prompt missing %q:\n%s", want, prompt)
		}
	}
}

func TestTruncateCommitDiff(t *testing.T) {
	diff, truncated := truncateCommitDiff("abcdef", 3)

	if !truncated {
		t.Fatal("expected truncation")
	}
	if !strings.Contains(diff, "abc") || !strings.Contains(diff, "diff truncated") || strings.Contains(diff, "def") {
		t.Fatalf("diff = %q", diff)
	}
}

func runGitForTest(t *testing.T, dir string, args ...string) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, output)
	}
}
