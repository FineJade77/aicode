package usagecmd

import "testing"

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
