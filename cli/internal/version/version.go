// Package version exposes the CLI build version.
package version

import "strings"

// Value is replaced by Makefile builds through -ldflags.
var Value = "dev"

func Current() string {
	value := strings.TrimSpace(Value)
	if value == "" {
		return "dev"
	}
	return value
}
