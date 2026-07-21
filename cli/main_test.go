package main

import (
	"reflect"
	"testing"
)

func TestParseGlobalArgsSupportsSandbox(t *testing.T) {
	options, args, err := parseGlobalArgs([]string{"--sandbox", "docker", "lint"})
	if err != nil {
		t.Fatal(err)
	}
	if options.Sandbox != "docker" {
		t.Fatalf("sandbox = %q", options.Sandbox)
	}
	if !reflect.DeepEqual(args, []string{"lint"}) {
		t.Fatalf("args = %#v", args)
	}
}

func TestParseGlobalArgsReportsMissingSandboxValue(t *testing.T) {
	if _, _, err := parseGlobalArgs([]string{"--sandbox"}); err == nil {
		t.Fatal("expected error")
	}
}
