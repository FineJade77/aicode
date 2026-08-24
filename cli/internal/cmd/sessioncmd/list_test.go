package sessioncmd

import (
	"strings"
	"testing"
)

func TestParseArgsListDefaults(t *testing.T) {
	opts, err := ParseArgs(nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if opts.Prune || opts.Limit != nil || opts.Offset != nil {
		t.Fatalf("expected a bare listing, got %+v", opts)
	}
	if path := listPath(opts); path != "/v1/sessions" {
		t.Fatalf("expected no query string, got %q", path)
	}
}

func TestParseArgsListPagination(t *testing.T) {
	for _, args := range [][]string{
		{"--limit", "10", "--offset", "20"},
		{"--limit=10", "--offset=20"},
	} {
		opts, err := ParseArgs(args)
		if err != nil {
			t.Fatalf("%v: unexpected error: %v", args, err)
		}
		if *opts.Limit != 10 || *opts.Offset != 20 {
			t.Fatalf("%v: expected limit=10 offset=20, got %+v", args, opts)
		}
		if path := listPath(opts); path != "/v1/sessions?limit=10&offset=20" {
			t.Fatalf("%v: unexpected path %q", args, path)
		}
	}
}

func TestParseArgsPrune(t *testing.T) {
	opts, err := ParseArgs([]string{"prune", "--max-sessions", "50", "--max-age-days", "30"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !opts.Prune || *opts.MaxSessions != 50 || *opts.MaxAgeDays != 30 {
		t.Fatalf("unexpected options: %+v", opts)
	}
}

func TestParseArgsPruneWithoutBoundsDefersToConfiguredPolicy(t *testing.T) {
	opts, err := ParseArgs([]string{"prune"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !opts.Prune || opts.MaxSessions != nil || opts.MaxAgeDays != nil {
		t.Fatalf("bare prune must leave bounds unset so the Runtime applies its policy, got %+v", opts)
	}
}

func TestParseArgsRejectsMisplacedFlags(t *testing.T) {
	cases := map[string][]string{
		"retention flags on a listing": {"--max-sessions", "5"},
		"pagination flags on prune":    {"prune", "--limit", "5"},
		"unknown flag":                 {"--nope"},
		"missing value":                {"--limit"},
		"negative value":               {"--limit", "-1"},
		"non-numeric value":            {"--limit", "many"},
	}
	for name, args := range cases {
		if _, err := ParseArgs(args); err == nil {
			t.Fatalf("%s: expected an error for %v", name, args)
		}
	}
}

func TestParseArgsErrorsIncludeUsage(t *testing.T) {
	_, err := ParseArgs([]string{"--nope"})
	if err == nil || !strings.Contains(err.Error(), "aicode session prune") {
		t.Fatalf("error should show usage, got %v", err)
	}
}
