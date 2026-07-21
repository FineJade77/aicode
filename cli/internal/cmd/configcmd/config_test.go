package configcmd

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
