package daemon

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGenerateTokenProducesDistinctHexStrings(t *testing.T) {
	first, err := generateToken()
	if err != nil {
		t.Fatalf("generateToken failed: %v", err)
	}
	second, err := generateToken()
	if err != nil {
		t.Fatalf("generateToken failed: %v", err)
	}
	if len(first) != 64 {
		t.Fatalf("expected 64 hex chars (32 bytes), got %d: %q", len(first), first)
	}
	if first == second {
		t.Fatalf("expected two calls to generateToken to differ, both were %q", first)
	}
}

func TestTokenReadsPersistedFile(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)

	if err := os.WriteFile(tokenPath(home), []byte("abc123\n"), 0o600); err != nil {
		t.Fatalf("failed to seed token file: %v", err)
	}

	got := Token()
	if got != "abc123" {
		t.Fatalf("expected Token() to return %q, got %q", "abc123", got)
	}
}

func TestTokenReturnsEmptyWhenFileMissing(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)

	got := Token()
	if got != "" {
		t.Fatalf("expected empty token when file is missing, got %q", got)
	}
}

func TestTokenPath(t *testing.T) {
	got := tokenPath(filepath.Join("home", ".aicode"))
	want := filepath.Join("home", ".aicode", "runtime.token")
	if got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
}

func TestRuntimeInstallationFromManifest(t *testing.T) {
	installRoot := t.TempDir()
	versionRoot := filepath.Join(installRoot, "0.1.0")
	runtimeDir := filepath.Join(versionRoot, "runtime")
	python := filepath.Join(versionRoot, "venv", "bin", "python")
	writeRuntimeEntrypoint(t, runtimeDir)
	if err := os.MkdirAll(filepath.Dir(python), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(python, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	manifestPath := writeInstallManifest(t, installRoot, installManifest{
		SchemaVersion: installManifestSchemaVersion,
		Version:       "0.1.0",
		RuntimeDir:    "0.1.0/runtime",
		Python:        "0.1.0/venv/bin/python",
	})

	installation, err := runtimeInstallationFromManifest(manifestPath)
	if err != nil {
		t.Fatal(err)
	}
	if installation.RuntimeDir != runtimeDir {
		t.Fatalf("runtime dir = %q, want %q", installation.RuntimeDir, runtimeDir)
	}
	if installation.Python != python {
		t.Fatalf("python = %q, want %q", installation.Python, python)
	}
	if installation.Version != "0.1.0" || installation.Source != "install-manifest" {
		t.Fatalf("installation = %#v", installation)
	}
}

func TestRuntimeInstallationRejectsManifestTraversal(t *testing.T) {
	installRoot := t.TempDir()
	manifestPath := writeInstallManifest(t, installRoot, installManifest{
		SchemaVersion: installManifestSchemaVersion,
		Version:       "0.1.0",
		RuntimeDir:    "../runtime",
		Python:        "0.1.0/venv/bin/python",
	})

	_, err := runtimeInstallationFromManifest(manifestPath)
	if err == nil || !strings.Contains(err.Error(), "不能逃逸安装目录") {
		t.Fatalf("expected traversal error, got %v", err)
	}
}

func TestResolveRuntimePrefersExplicitDirectory(t *testing.T) {
	runtimeDir := filepath.Join(t.TempDir(), "runtime")
	writeRuntimeEntrypoint(t, runtimeDir)
	t.Setenv("AICODE_RUNTIME_DIR", runtimeDir)
	t.Setenv("AICODE_RUNTIME_PYTHON", "/custom/python")
	t.Setenv("AICODE_RUNTIME_VERSION", "dev")

	installation, err := ResolveRuntime()
	if err != nil {
		t.Fatal(err)
	}
	if installation.RuntimeDir != runtimeDir || installation.Python != "/custom/python" {
		t.Fatalf("installation = %#v", installation)
	}
	if installation.Version != "dev" || installation.Source != "environment" {
		t.Fatalf("installation = %#v", installation)
	}
}

func TestSourceRuntimeFromNestedDirectory(t *testing.T) {
	root := t.TempDir()
	runtimeDir := filepath.Join(root, "runtime")
	nested := filepath.Join(root, "workspace", "nested")
	writeRuntimeEntrypoint(t, runtimeDir)
	if err := os.MkdirAll(nested, 0o755); err != nil {
		t.Fatal(err)
	}

	resolved, err := sourceRuntimeFrom(nested)
	if err != nil {
		t.Fatal(err)
	}
	if resolved != runtimeDir {
		t.Fatalf("runtime dir = %q, want %q", resolved, runtimeDir)
	}
}

func TestSourceRuntimeVersionReadsRepositoryVersion(t *testing.T) {
	root := t.TempDir()
	runtimeDir := filepath.Join(root, "runtime")
	if err := os.MkdirAll(runtimeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "VERSION"), []byte("1.2.3\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if got := sourceRuntimeVersion(runtimeDir); got != "1.2.3" {
		t.Fatalf("version = %q, want 1.2.3", got)
	}

	t.Setenv("AICODE_RUNTIME_VERSION", "dev")
	if got := sourceRuntimeVersion(runtimeDir); got != "dev" {
		t.Fatalf("override version = %q, want dev", got)
	}
}

func TestManifestPathForInstalledExecutable(t *testing.T) {
	prefix := t.TempDir()
	executable := filepath.Join(prefix, "bin", "aicode")
	want := filepath.Join(prefix, "lib", "aicode", "manifest.json")

	if got := manifestPathForExecutable(executable); got != want {
		t.Fatalf("manifest path = %q, want %q", got, want)
	}
}

func writeRuntimeEntrypoint(t *testing.T, runtimeDir string) {
	t.Helper()
	entrypoint := filepath.Join(runtimeDir, "app", "server", "main.py")
	if err := os.MkdirAll(filepath.Dir(entrypoint), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(entrypoint, []byte("app = None\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func writeInstallManifest(t *testing.T, root string, manifest installManifest) string {
	t.Helper()
	content, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "manifest.json")
	if err := os.WriteFile(path, content, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}
