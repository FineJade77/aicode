package sessioncmd

import (
	"strings"
	"testing"

	"github.com/FineJade77/aicode/cli/internal/config"
)

func TestResolveSessionIDUsesExplicitID(t *testing.T) {
	sessionID, err := resolveSessionID(config.Config{}, []string{"sess_123"})
	if err != nil {
		t.Fatal(err)
	}
	if sessionID != "sess_123" {
		t.Fatalf("sessionID = %q", sessionID)
	}
}

func TestResolveSessionIDRequiresOneTarget(t *testing.T) {
	_, err := resolveSessionID(config.Config{}, nil)
	if err == nil || !strings.Contains(err.Error(), "aicode session cancel") {
		t.Fatalf("err = %v", err)
	}
}
