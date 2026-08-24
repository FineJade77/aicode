package projectcmd

import (
	"strings"
	"testing"
)

func TestSupportedSandboxActions(t *testing.T) {
	for _, action := range []string{"test", "build", "lint"} {
		if !supportedSandboxAction(action) {
			t.Fatalf("expected %q to be supported", action)
		}
	}
	if supportedSandboxAction("shell") {
		t.Fatal("arbitrary sandbox shell action must not be supported")
	}
}

func TestNewExecutionID(t *testing.T) {
	first, err := newExecutionID()
	if err != nil {
		t.Fatal(err)
	}
	second, err := newExecutionID()
	if err != nil {
		t.Fatal(err)
	}
	if first == second || !strings.HasPrefix(first, "exec_") || len(first) != 37 {
		t.Fatalf("execution ids = %q, %q", first, second)
	}
}
