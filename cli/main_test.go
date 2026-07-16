package main

import (
	"reflect"
	"testing"
)

func TestReviewRuleIDsSortsAndDeduplicates(t *testing.T) {
	ids := reviewRuleIDs(map[string]any{
		"rules": []any{
			map[string]any{"id": "large_diff"},
			map[string]any{"id": " debug_output "},
			map[string]any{"id": "large_diff"},
			map[string]any{"id": ""},
			"not-a-rule",
		},
	})

	want := []string{"debug_output", "large_diff"}
	if !reflect.DeepEqual(ids, want) {
		t.Fatalf("ids = %#v, want %#v", ids, want)
	}
}

func TestReviewRuleIDsHandlesMalformedPayload(t *testing.T) {
	if ids := reviewRuleIDs(map[string]any{"rules": "bad"}); len(ids) != 0 {
		t.Fatalf("ids = %#v", ids)
	}
	if ids := reviewRuleIDs("bad"); len(ids) != 0 {
		t.Fatalf("ids = %#v", ids)
	}
}

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
