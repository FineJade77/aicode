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

func TestParseForkArgsSeparatesTheMessageFlag(t *testing.T) {
	rest, messageID, err := parseForkArgs([]string{"sess_1", "--message", "12"})
	if err != nil {
		t.Fatal(err)
	}
	if len(rest) != 1 || rest[0] != "sess_1" {
		t.Fatalf("rest = %#v", rest)
	}
	if messageID == nil || *messageID != 12 {
		t.Fatalf("messageID = %#v", messageID)
	}
}

func TestParseForkArgsAcceptsTheEqualsForm(t *testing.T) {
	_, messageID, err := parseForkArgs([]string{"--message=4", "sess_1"})
	if err != nil {
		t.Fatal(err)
	}
	if messageID == nil || *messageID != 4 {
		t.Fatalf("messageID = %#v", messageID)
	}
}

func TestParseForkArgsWithoutTheFlagForksAtTheTip(t *testing.T) {
	rest, messageID, err := parseForkArgs([]string{"--last"})
	if err != nil {
		t.Fatal(err)
	}
	if messageID != nil {
		t.Fatalf("messageID = %#v, want nil", messageID)
	}
	if len(rest) != 1 || rest[0] != "--last" {
		t.Fatalf("rest = %#v", rest)
	}
}

func TestParseForkArgsRejectsANonNumericMessage(t *testing.T) {
	if _, _, err := parseForkArgs([]string{"sess_1", "--message", "abc"}); err == nil {
		t.Fatal("expected an error for a non-numeric message id")
	}
}
