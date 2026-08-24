package taskcmd

import (
	"fmt"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

const maxPRDiffChars = 80_000

// defaultBaseCandidates is consulted in order when the user does not name a base.
//
// Guessing wrong is cheap to correct and expensive to prevent: a repository can
// call its trunk anything. The list covers the common names and the command says
// which one it used, so a wrong guess is visible rather than silent.
var defaultBaseCandidates = []string{"origin/main", "origin/master", "main", "master"}

type prContext struct {
	Base      string
	Branch    string
	Commits   string
	Stat      string
	Diff      string
	Truncated bool
}

func runPRDescription(cfg config.Config, args []string) error {
	base := ""
	if len(args) == 2 && args[0] == "--base" {
		base = strings.TrimSpace(args[1])
	} else if len(args) != 0 {
		return fmt.Errorf("usage: aicode task pr-description [--base <ref>]")
	}

	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	prc, err := collectPRContext(root.Path, base)
	if err != nil {
		return err
	}
	// Printed before the model runs: the description is only as good as the
	// range it describes, and a silently wrong base produces a confident
	// description of the wrong changes.
	fmt.Printf("Comparing %s...%s\n\n", prc.Base, prc.Branch)
	return RunPrompt(cfg, "commit_message", prDescriptionPrompt(prc))
}

func collectPRContext(workspacePath string, base string) (prContext, error) {
	branch, err := gitOutput(workspacePath, "rev-parse", "--abbrev-ref", "HEAD")
	if err != nil {
		return prContext{}, err
	}
	branch = strings.TrimSpace(branch)

	if base == "" {
		base = detectBase(workspacePath, branch)
	}
	if base == "" {
		return prContext{}, fmt.Errorf(
			"could not determine a base branch; pass one with --base <ref>",
		)
	}
	if _, err := gitOutput(workspacePath, "rev-parse", "--verify", base); err != nil {
		return prContext{}, fmt.Errorf("base %q does not resolve to a revision", base)
	}

	// The two ranges are deliberately different, because git's notation means
	// opposite things to the two commands:
	//
	//   log  base..branch   commits on the branch and not on the base
	//   diff base...branch  changes since the merge base
	//
	// `log base...branch` is the *symmetric* difference and would list the
	// base's own commits as if they belonged to this pull request; `diff
	// base..branch` would show the base's changes inverted. Using one spec for
	// both reads as consistency and is wrong in one of the two places.
	logSpec := base + ".." + branch
	diffSpec := base + "..." + branch
	commits, err := gitOutput(workspacePath, "log", "--no-merges", "--pretty=format:- %s", logSpec)
	if err != nil {
		return prContext{}, err
	}
	if strings.TrimSpace(commits) == "" {
		return prContext{}, fmt.Errorf("no commits between %s and %s", base, branch)
	}
	stat, err := gitOutput(workspacePath, "diff", "--stat", "--no-ext-diff", diffSpec)
	if err != nil {
		return prContext{}, err
	}
	diff, err := gitOutput(workspacePath, "diff", "--no-ext-diff", diffSpec)
	if err != nil {
		return prContext{}, err
	}
	truncated, wasTruncated := truncateCommitDiff(diff, maxPRDiffChars)
	return prContext{
		Base:      base,
		Branch:    branch,
		Commits:   strings.TrimSpace(commits),
		Stat:      strings.TrimSpace(stat),
		Diff:      truncated,
		Truncated: wasTruncated,
	}, nil
}

func detectBase(workspacePath string, branch string) string {
	for _, candidate := range defaultBaseCandidates {
		if candidate == branch {
			// Comparing a branch with itself yields an empty range, which would
			// be reported as "no commits" instead of "you are on the base".
			continue
		}
		if _, err := gitOutput(workspacePath, "rev-parse", "--verify", candidate); err == nil {
			return candidate
		}
	}
	return ""
}

func prDescriptionPrompt(prc prContext) string {
	return fmt.Sprintf(`Write a pull request description for the following branch.

Requirements:
- Output only the description in Markdown, with no surrounding commentary.
- Start with a one-paragraph summary of what changes and why.
- Then a "## Changes" section with concise bullets grouped by area.
- Add a "## Notes for reviewers" section only when something genuinely needs
  attention: a risky area, a deliberate omission, or a decision worth flagging.
- Do not invent testing that the diff does not show, and do not describe intent
  the diff does not support.

Base: %s
Branch: %s
Diff truncated: %v

Commits:
%s

Diff stat:
%s

Diff:
%s`, prc.Base, prc.Branch, prc.Truncated, emptyFallback(prc.Commits, "(none)"), emptyFallback(prc.Stat, "(empty)"), prc.Diff)
}
