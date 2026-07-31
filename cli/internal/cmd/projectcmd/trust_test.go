package projectcmd

import "testing"

func TestParseArgs(t *testing.T) {
	tests := []struct {
		args       []string
		wantAction string
		wantJSON   bool
	}{
		{nil, "status", false},
		{[]string{"status"}, "status", false},
		{[]string{"add", "--json"}, "add", true},
		{[]string{"--json", "list"}, "list", true},
		{[]string{"remove"}, "remove", false},
	}
	for _, test := range tests {
		action, jsonOutput, err := parseArgs(test.args)
		if err != nil {
			t.Fatalf("parseArgs(%v): %v", test.args, err)
		}
		if action != test.wantAction || jsonOutput != test.wantJSON {
			t.Fatalf("parseArgs(%v) = %q, %v", test.args, action, jsonOutput)
		}
	}
	if _, _, err := parseArgs([]string{"add", "extra"}); err == nil {
		t.Fatal("expected extra argument to fail")
	}
}
