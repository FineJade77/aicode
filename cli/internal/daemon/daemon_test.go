package daemon

import (
	"os"
	"path/filepath"
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
