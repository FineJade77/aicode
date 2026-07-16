package renderer

import "testing"

func TestUsageLineIncludesPurpose(t *testing.T) {
	line := usageLine(map[string]any{
		"purpose":       "reviewer",
		"model":         "stub",
		"input_tokens":  12,
		"output_tokens": 3,
	})

	want := "用量: purpose=reviewer model=stub input=12 output=3"
	if line != want {
		t.Fatalf("usageLine() = %q, want %q", line, want)
	}
}

func TestUsageLineDefaultsMissingPurpose(t *testing.T) {
	line := usageLine(map[string]any{
		"model":         "stub",
		"input_tokens":  12,
		"output_tokens": 3,
	})

	want := "用量: purpose=unknown model=stub input=12 output=3"
	if line != want {
		t.Fatalf("usageLine() = %q, want %q", line, want)
	}
}
