package projectcmd

import (
	"strings"
	"testing"

	"github.com/FineJade77/aicode/cli/internal/config"
)

func TestHelpGroupsProjectScopedCommands(t *testing.T) {
	for _, command := range []string{"trust", "review", "protected", "command", "workspace", "sandbox"} {
		if !strings.Contains(HelpText, command) {
			t.Fatalf("project help is missing %q", command)
		}
	}
}

func TestCommandRequiresTestSubcommand(t *testing.T) {
	err := Run(config.Config{}, []string{"command", "build"})
	if err == nil || !strings.Contains(err.Error(), "project command test") {
		t.Fatalf("error = %v", err)
	}
}
