package taskcmd

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func repoWithBranch(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not available")
	}
	root := t.TempDir()
	runGitForTest(t, root, "init")
	// Older git versions default to `master` and reject --initial-branch.
	runGitForTest(t, root, "symbolic-ref", "HEAD", "refs/heads/main")
	runGitForTest(t, root, "config", "user.email", "test@example.com")
	runGitForTest(t, root, "config", "user.name", "Test")
	write(t, root, "base.txt", "base\n")
	runGitForTest(t, root, "add", ".")
	runGitForTest(t, root, "commit", "-m", "base commit")
	runGitForTest(t, root, "checkout", "-b", "feature")
	write(t, root, "feature.txt", "feature\n")
	runGitForTest(t, root, "add", ".")
	runGitForTest(t, root, "commit", "-m", "add feature")
	return root
}

func write(t *testing.T, root string, name string, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(root, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestPRContextDescribesTheBranchAgainstItsBase(t *testing.T) {
	root := repoWithBranch(t)

	prc, err := collectPRContext(root, "main")
	if err != nil {
		t.Fatal(err)
	}

	if prc.Branch != "feature" || prc.Base != "main" {
		t.Fatalf("context = %#v", prc)
	}
	if !strings.Contains(prc.Commits, "add feature") {
		t.Fatalf("commits = %q", prc.Commits)
	}
	if strings.Contains(prc.Commits, "base commit") {
		t.Fatalf("the base's own history is not part of this pull request: %q", prc.Commits)
	}
	if !strings.Contains(prc.Diff, "feature.txt") {
		t.Fatalf("diff = %q", prc.Diff)
	}
}

func TestPRContextExcludesCommitsMadeOnTheBaseAfterDiverging(t *testing.T) {
	// The three-dot range is the whole point: `base..branch` would describe
	// other people's commits as if they belonged to this pull request.
	root := repoWithBranch(t)
	runGitForTest(t, root, "checkout", "main")
	write(t, root, "unrelated.txt", "someone else\n")
	runGitForTest(t, root, "add", ".")
	runGitForTest(t, root, "commit", "-m", "unrelated work on main")
	runGitForTest(t, root, "checkout", "feature")

	prc, err := collectPRContext(root, "main")
	if err != nil {
		t.Fatal(err)
	}

	if strings.Contains(prc.Commits, "unrelated work") {
		t.Fatalf("commits = %q", prc.Commits)
	}
	if strings.Contains(prc.Diff, "unrelated.txt") {
		t.Fatalf("diff = %q", prc.Diff)
	}
}

func TestPRContextRejectsAnUnknownBase(t *testing.T) {
	root := repoWithBranch(t)

	if _, err := collectPRContext(root, "no-such-ref"); err == nil {
		t.Fatal("an unresolvable base must fail rather than describe nothing")
	}
}

func TestPRContextRefusesWhenThereIsNothingToDescribe(t *testing.T) {
	root := repoWithBranch(t)
	runGitForTest(t, root, "checkout", "main")

	_, err := collectPRContext(root, "main")

	if err == nil || !strings.Contains(err.Error(), "no commits") {
		t.Fatalf("err = %v", err)
	}
}

func TestBaseDetectionSkipsTheBranchYouAreOn(t *testing.T) {
	// On `main` with no `origin/*`, `main` itself must not be chosen: comparing a
	// branch with itself yields an empty range, reported as "no commits" instead
	// of the truthful "you are on the base".
	root := repoWithBranch(t)
	runGitForTest(t, root, "checkout", "main")

	if base := detectBase(root, "main"); base == "main" {
		t.Fatalf("base = %q", base)
	}
}
