package sandboxcmd

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestDockerSandboxArgsAreIsolated(t *testing.T) {
	args := dockerSandboxArgs("/repo", "golang:1.22", "go test ./...", nil, dockerSandboxLimits{CPUs: "2", Memory: "2g", PIDsLimit: "256"})
	want := []string{
		"run",
		"--rm",
		"--network",
		"none",
		"--cpus",
		"2",
		"--memory",
		"2g",
		"--pids-limit",
		"256",
		"--env",
		"AICODE_SANDBOX=1",
		"--env",
		"HOME=/tmp/aicode-home",
		"--env",
		"XDG_CACHE_HOME=/tmp/aicode-cache",
		"--env",
		"GOCACHE=/tmp/aicode-go-build",
		"--env",
		"GOMODCACHE=/tmp/aicode-go-mod",
		"--env",
		"npm_config_cache=/tmp/aicode-npm-cache",
		"--env",
		"YARN_CACHE_FOLDER=/tmp/aicode-yarn-cache",
		"--env",
		"PIP_CACHE_DIR=/tmp/aicode-pip-cache",
		"--mount",
		"type=bind,src=/repo,dst=/workspace,readonly",
		"-w",
		"/workspace",
		"golang:1.22",
		"sh",
		"-lc",
		"go test ./...",
	}
	if !reflect.DeepEqual(args, want) {
		t.Fatalf("dockerSandboxArgs() = %#v, want %#v", args, want)
	}
	if strings.Contains(strings.Join(args, " "), "--env-file") {
		t.Fatalf("docker args should not pass env files: %#v", args)
	}
}

func TestDockerSandboxArgsMasksEnvFiles(t *testing.T) {
	args := dockerSandboxArgs("/repo", "golang:1.22", "go test ./...", []sandboxMount{
		{Source: "/tmp/empty-env", Target: "/workspace/.env"},
		{Source: "/tmp/empty-env", Target: "/workspace/.env.local"},
	}, dockerSandboxLimits{CPUs: "2", Memory: "2g", PIDsLimit: "256"})
	joined := strings.Join(args, " ")
	for _, want := range []string{
		"type=bind,src=/tmp/empty-env,dst=/workspace/.env,readonly",
		"type=bind,src=/tmp/empty-env,dst=/workspace/.env.local,readonly",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("docker args %q missing %q", joined, want)
		}
	}
}

func TestDockerSandboxLimitsSupportEnvOverrides(t *testing.T) {
	t.Setenv("AICODE_SANDBOX_CPUS", "1.5")
	t.Setenv("AICODE_SANDBOX_MEMORY", "768m")
	t.Setenv("AICODE_SANDBOX_PIDS_LIMIT", "128")

	limits := defaultDockerSandboxLimits()

	if limits.CPUs != "1.5" || limits.Memory != "768m" || limits.PIDsLimit != "128" {
		t.Fatalf("limits = %#v", limits)
	}
}

func TestSandboxEnvFilesFindsRootEnvFiles(t *testing.T) {
	root := t.TempDir()
	for _, name := range []string{".env", ".env.local", ".env.test"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("secret=1\n"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(root, ".env.d"), 0o755); err != nil {
		t.Fatal(err)
	}

	got, err := sandboxEnvFiles(root)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{".env", ".env.local", ".env.test"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("sandboxEnvFiles() = %#v, want %#v", got, want)
	}
}

func TestDockerSandboxEnvMasksCreatesEmptyFileMasks(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, ".env"), []byte("secret=1\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	masks, cleanup, err := dockerSandboxEnvMasks(root)
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()

	if len(masks) != 1 {
		t.Fatalf("masks = %#v", masks)
	}
	if masks[0].Target != "/workspace/.env" {
		t.Fatalf("mask target = %q", masks[0].Target)
	}
	content, err := os.ReadFile(masks[0].Source)
	if err != nil {
		t.Fatal(err)
	}
	if len(content) != 0 {
		t.Fatalf("mask source content = %q", content)
	}
}

func TestDockerSandboxImageDefaultsByCommand(t *testing.T) {
	t.Setenv("AICODE_SANDBOX_DOCKER_IMAGE", "")
	tests := map[string]string{
		"go test ./...":       "golang:1.22",
		"npm test":            "node:22",
		"python3 -m pytest":   "python:3.12-slim",
		"make test-in-docker": "ubuntu:24.04",
	}
	for command, want := range tests {
		if got := dockerSandboxImage(command); got != want {
			t.Fatalf("dockerSandboxImage(%q) = %q, want %q", command, got, want)
		}
	}
}

func TestDockerSandboxImageSupportsOverride(t *testing.T) {
	t.Setenv("AICODE_SANDBOX_DOCKER_IMAGE", "custom:test")
	if got := dockerSandboxImage("go test ./..."); got != "custom:test" {
		t.Fatalf("dockerSandboxImage() = %q", got)
	}
}

func TestDetectSandboxTestCommandUsesConfiguredCommand(t *testing.T) {
	root := t.TempDir()
	writeProjectConfig(t, root, `{"commands":{"test":"go test ./cli/..."}}`)

	command, err := detectSandboxTestCommand(root)
	if err != nil {
		t.Fatal(err)
	}
	if command != "go test ./cli/..." {
		t.Fatalf("command = %q", command)
	}
}

func TestDetectSandboxCommandUsesConfiguredBuildCommand(t *testing.T) {
	root := t.TempDir()
	writeProjectConfig(t, root, `{"commands":{"build":"npm run build:ci"}}`)

	command, err := detectSandboxCommand(root, "build")
	if err != nil {
		t.Fatal(err)
	}
	if command != "npm run build:ci" {
		t.Fatalf("command = %q", command)
	}
}

func TestDetectSandboxTestCommandHandlesAutoGoWork(t *testing.T) {
	root := t.TempDir()
	writeProjectConfig(t, root, `{"commands":{"test":"auto"}}`)
	if err := os.WriteFile(filepath.Join(root, "go.work"), []byte("go 1.22\n\nuse (\n\t./cli\n\t./runtime\n)\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	command, err := detectSandboxTestCommand(root)
	if err != nil {
		t.Fatal(err)
	}
	if command != "go test ./cli/... ./runtime/..." {
		t.Fatalf("command = %q", command)
	}
}

func TestDetectSandboxBuildCommandHandlesAutoGoWork(t *testing.T) {
	root := t.TempDir()
	writeProjectConfig(t, root, `{"commands":{"build":"auto"}}`)
	if err := os.WriteFile(filepath.Join(root, "go.work"), []byte("go 1.22\n\nuse (\n\t./cli\n\t./runtime\n)\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	command, err := detectSandboxCommand(root, "build")
	if err != nil {
		t.Fatal(err)
	}
	if command != "go build ./cli/... ./runtime/..." {
		t.Fatalf("command = %q", command)
	}
}

func TestDetectSandboxLintCommandDetectsPackageScript(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "package.json"), []byte(`{"scripts":{"lint":"eslint ."}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "pnpm-lock.yaml"), []byte("lockfileVersion: '9.0'\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	command, err := detectSandboxCommand(root, "lint")
	if err != nil {
		t.Fatal(err)
	}
	if command != "pnpm lint" {
		t.Fatalf("command = %q", command)
	}
}

func TestDetectSandboxLintCommandDetectsRuff(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "pyproject.toml"), []byte("[tool.ruff]\nline-length = 120\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	command, err := detectSandboxCommand(root, "lint")
	if err != nil {
		t.Fatal(err)
	}
	if command != "python3 -m ruff check ." {
		t.Fatalf("command = %q", command)
	}
}

func TestDetectSandboxTestCommandDetectsPython(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "pyproject.toml"), []byte("[project]\nname = \"demo\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	command, err := detectSandboxTestCommand(root)
	if err != nil {
		t.Fatal(err)
	}
	if command != "python3 -m pytest" {
		t.Fatalf("command = %q", command)
	}
}

func TestDetectPackageTestCommandRespectsMissingScript(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "package.json"), []byte(`{"scripts":{"lint":"eslint ."}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if command := detectPackageTestCommand(root); command != "" {
		t.Fatalf("command = %q", command)
	}
}

func TestSandboxAuditEventWritesJSONLWithoutRawCommand(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)
	limits := dockerSandboxLimits{CPUs: "2", Memory: "2g", PIDsLimit: "256"}

	err := appendAuditEvent("sandbox.started", "/repo", sandboxAuditData("lint", "node:22", "npm run lint -- --token secret", limits, 2, nil))
	if err != nil {
		t.Fatal(err)
	}

	content, err := os.ReadFile(filepath.Join(home, "audit.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	var event map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(content))), &event); err != nil {
		t.Fatal(err)
	}
	if event["event_type"] != "sandbox.started" || event["workspace"] != "/repo" {
		t.Fatalf("event = %#v", event)
	}
	data := event["data"].(map[string]any)
	if data["action"] != "lint" || data["workspace_mode"] != "read_only" || data["network"] != "none" {
		t.Fatalf("data = %#v", data)
	}
	if data["command_hash"] == "" {
		t.Fatalf("missing command hash: %#v", data)
	}
	if strings.Contains(string(content), "secret") || strings.Contains(string(content), "npm run lint") {
		t.Fatalf("audit leaked raw command: %s", content)
	}
}

func writeProjectConfig(t *testing.T, root string, content string) {
	t.Helper()
	configDir := filepath.Join(root, ".aicode")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(configDir, "config.json"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
