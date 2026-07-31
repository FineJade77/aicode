package configcmd

import (
	"strings"
	"testing"
)

func TestHelpContainsOnlyGlobalConfiguration(t *testing.T) {
	for _, command := range []string{"init", "show", "get", "set", "unset", "docs"} {
		if !strings.Contains(HelpText, command) {
			t.Fatalf("config help is missing %q", command)
		}
	}
	for _, projectCommand := range []string{"protected", "review", "workspace"} {
		if strings.Contains(HelpText, projectCommand) {
			t.Fatalf("config help exposes project command %q", projectCommand)
		}
	}
}
