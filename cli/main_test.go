package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestParseGlobalArgsSupportsSandbox(t *testing.T) {
	options, args, err := parseGlobalArgs([]string{"--sandbox", "docker", "test"})
	if err != nil {
		t.Fatal(err)
	}
	if options.Sandbox != "docker" {
		t.Fatalf("sandbox = %q", options.Sandbox)
	}
	if !reflect.DeepEqual(args, []string{"test"}) {
		t.Fatalf("args = %#v", args)
	}
}

func TestParseGlobalArgsReportsMissingSandboxValue(t *testing.T) {
	if _, _, err := parseGlobalArgs([]string{"--sandbox"}); err == nil {
		t.Fatal("expected error")
	}
}

func TestDockerSandboxArgsAreIsolated(t *testing.T) {
	args := dockerSandboxArgs("/repo", "golang:1.22", "go test ./...", nil)
	want := []string{
		"run",
		"--rm",
		"--network",
		"none",
		"--env",
		"AICODE_SANDBOX=1",
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
	})
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

func TestReviewRuleIDsSortsAndDeduplicates(t *testing.T) {
	ids := reviewRuleIDs(map[string]any{
		"rules": []any{
			map[string]any{"id": "large_diff"},
			map[string]any{"id": " debug_output "},
			map[string]any{"id": "large_diff"},
			map[string]any{"id": ""},
			"not-a-rule",
		},
	})

	want := []string{"debug_output", "large_diff"}
	if !reflect.DeepEqual(ids, want) {
		t.Fatalf("ids = %#v, want %#v", ids, want)
	}
}

func TestReviewRuleIDsHandlesMalformedPayload(t *testing.T) {
	if ids := reviewRuleIDs(map[string]any{"rules": "bad"}); len(ids) != 0 {
		t.Fatalf("ids = %#v", ids)
	}
	if ids := reviewRuleIDs("bad"); len(ids) != 0 {
		t.Fatalf("ids = %#v", ids)
	}
}

func TestUsagePath(t *testing.T) {
	tests := []struct {
		name     string
		args     []string
		wantPath string
		wantJSON bool
		wantErr  bool
	}{
		{name: "default", args: nil, wantPath: "/v1/usage"},
		{name: "json", args: []string{"--json"}, wantPath: "/v1/usage", wantJSON: true},
		{name: "today", args: []string{"--today", "--json"}, wantPath: "/v1/usage?today=true", wantJSON: true},
		{name: "session", args: []string{"--session", "sess_1", "--json"}, wantPath: "/v1/usage/sessions/sess_1", wantJSON: true},
		{name: "bad", args: []string{"--session"}, wantErr: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			gotPath, gotJSON, err := usagePath(test.args)
			if test.wantErr {
				if err == nil {
					t.Fatal("expected error")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if gotPath != test.wantPath || gotJSON != test.wantJSON {
				t.Fatalf("usagePath() = %q %v, want %q %v", gotPath, gotJSON, test.wantPath, test.wantJSON)
			}
		})
	}
}

func TestParseResumeArgs(t *testing.T) {
	tests := []struct {
		name    string
		args    []string
		want    resumeTarget
		wantErr bool
	}{
		{name: "inspect last", args: []string{"--last"}, want: resumeTarget{UseLast: true, Inspect: true}},
		{name: "resume last with message", args: []string{"--last", "继续", "任务"}, want: resumeTarget{UseLast: true, Message: "继续 任务"}},
		{name: "inspect session", args: []string{"sess_1"}, want: resumeTarget{SessionID: "sess_1", Inspect: true}},
		{name: "resume session with message", args: []string{"sess_1", "继续", "任务"}, want: resumeTarget{SessionID: "sess_1", Message: "继续 任务"}},
		{name: "missing", args: nil, wantErr: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := parseResumeArgs(test.args)
			if test.wantErr {
				if err == nil {
					t.Fatal("expected error")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("parseResumeArgs() = %#v, want %#v", got, test.want)
			}
		})
	}
}

func TestSessionInfoFromValue(t *testing.T) {
	info, err := sessionInfoFromValue(map[string]any{
		"session_id": "sess_1",
		"workspace":  "/repo",
		"language":   "en-US",
	})
	if err != nil {
		t.Fatal(err)
	}
	want := sessionInfo{SessionID: "sess_1", Workspace: "/repo", Language: "en-US"}
	if info != want {
		t.Fatalf("sessionInfoFromValue() = %#v, want %#v", info, want)
	}
}

func TestSessionInfoFromValueDefaultsLanguage(t *testing.T) {
	info, err := sessionInfoFromValue(map[string]any{
		"session_id": "sess_1",
		"workspace":  "/repo",
	})
	if err != nil {
		t.Fatal(err)
	}
	if info.Language != "zh-CN" {
		t.Fatalf("language = %q", info.Language)
	}
}
