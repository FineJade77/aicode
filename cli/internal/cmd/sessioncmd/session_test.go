package sessioncmd

import (
	"strings"
	"testing"

	"github.com/FineJade77/aicode/cli/internal/config"
)

func TestResumeRequiresTargetAndMessage(t *testing.T) {
	err := Run(config.Config{}, []string{"resume", "--last"})
	if err == nil || !strings.Contains(err.Error(), "session resume") {
		t.Fatalf("error = %v", err)
	}
}

func TestShowRequiresOneTarget(t *testing.T) {
	err := Run(config.Config{}, []string{"show"})
	if err == nil || !strings.Contains(err.Error(), "session show") {
		t.Fatalf("error = %v", err)
	}
}
