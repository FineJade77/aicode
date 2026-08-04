package version

import (
	"os"
	"path/filepath"
	"regexp"
	"testing"
)

// The Go version is declared in three places that nothing keeps in sync:
// `go.work`, `cli/go.mod`, and the CI workflow. Drift is silent and asymmetric.
//
// If CI runs older than the `go` directive allows, nothing catches it — the
// directive is a minimum, so the build simply fails on a symbol CI's standard
// library does not have. If CI runs newer, the gate is weaker than it looks:
// vet's stdversion analyzer, which reports exactly that class of mistake, only
// exists from Go 1.23 on. Either way the failure lands in CI rather than in the
// editor, which is the expensive place to find it.
var (
	goDirective = regexp.MustCompile(`(?m)^go (\d+\.\d+)`)
	ciGoVersion = regexp.MustCompile(`go-version: "(\d+\.\d+)"`)
)

func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func readFile(t *testing.T, parts ...string) string {
	t.Helper()
	content, err := os.ReadFile(filepath.Join(parts...))
	if err != nil {
		t.Fatal(err)
	}
	return string(content)
}

func firstMatch(t *testing.T, pattern *regexp.Regexp, content string, what string) string {
	t.Helper()
	match := pattern.FindStringSubmatch(content)
	if match == nil {
		t.Fatalf("no %s found", what)
	}
	return match[1]
}

func TestGoVersionIsDeclaredConsistently(t *testing.T) {
	root := repoRoot(t)
	workspace := firstMatch(t, goDirective, readFile(t, root, "go.work"), "go directive in go.work")
	module := firstMatch(t, goDirective, readFile(t, root, "cli", "go.mod"), "go directive in cli/go.mod")

	if workspace != module {
		t.Fatalf("go.work says %s, cli/go.mod says %s", workspace, module)
	}

	workflow := readFile(t, root, ".github", "workflows", "ci.yml")
	matches := ciGoVersion.FindAllStringSubmatch(workflow, -1)
	if len(matches) == 0 {
		t.Fatal("no go-version found in the CI workflow")
	}
	for _, match := range matches {
		if match[1] != module {
			t.Fatalf("CI installs Go %s but the module declares %s", match[1], module)
		}
	}
}
