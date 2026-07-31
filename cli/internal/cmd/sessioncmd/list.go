package sessioncmd

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/renderer"
)

const listUsage = `Usage:
  aicode session list [--limit N] [--offset N]
  aicode session prune [--max-sessions N] [--max-age-days N]

Listings report message counts rather than full history; use the session
endpoint for a single session's messages.

prune deletes old sessions and their messages, events and compactions. With no
flags it applies the configured retention policy (AICODE_SESSION_RETENTION_*),
which is disabled by default. A session with a live run or an unresolved
approval is never deleted.`

// Options carries the parsed command line so parsing stays unit-testable.
type Options struct {
	Prune       bool
	Limit       *int
	Offset      *int
	MaxSessions *int
	MaxAgeDays  *int
}

func runList(cfg config.Config, args []string) error {
	opts, err := ParseArgs(args)
	if err != nil {
		return err
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}
	api := client.New(cfg.Runtime.URL, daemon.Token())
	ctx, cancel := context.WithTimeout(context.Background(), runtimeio.DefaultTimeout)
	defer cancel()

	if opts.Prune {
		result, err := api.PruneSessions(ctx, opts.MaxSessions, opts.MaxAgeDays)
		if err != nil {
			return err
		}
		renderer.PrintJSON(result)
		if result.Status == "disabled" {
			fmt.Println("Retention is disabled; pass --max-sessions or --max-age-days, or configure AICODE_SESSION_RETENTION_*.")
		}
		return nil
	}

	value, err := runtimeio.FetchJSON(cfg, listPath(opts))
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
}

func listPath(opts Options) string {
	query := []string{}
	if opts.Limit != nil {
		query = append(query, "limit="+strconv.Itoa(*opts.Limit))
	}
	if opts.Offset != nil {
		query = append(query, "offset="+strconv.Itoa(*opts.Offset))
	}
	if len(query) == 0 {
		return "/v1/sessions"
	}
	return "/v1/sessions?" + strings.Join(query, "&")
}

func ParseArgs(args []string) (Options, error) {
	opts := Options{}
	index := 0
	if len(args) > 0 && args[0] == "prune" {
		opts.Prune = true
		index = 1
	}
	for ; index < len(args); index++ {
		arg := args[index]
		name, value, hasInline := strings.Cut(arg, "=")
		target, known := flagTarget(&opts, name)
		if !known {
			return Options{}, fmt.Errorf("unknown session list/prune flag: %s\n\n%s", arg, listUsage)
		}
		if !hasInline {
			if index+1 >= len(args) {
				return Options{}, fmt.Errorf("%s requires a value\n\n%s", name, listUsage)
			}
			index++
			value = args[index]
		}
		parsed, err := strconv.Atoi(strings.TrimSpace(value))
		if err != nil || parsed < 0 {
			return Options{}, fmt.Errorf("%s expects a non-negative integer, got %q", name, value)
		}
		*target = &parsed
	}
	if !opts.Prune && (opts.MaxSessions != nil || opts.MaxAgeDays != nil) {
		return Options{}, fmt.Errorf("--max-sessions and --max-age-days only apply to `aicode session prune`\n\n%s", listUsage)
	}
	if opts.Prune && (opts.Limit != nil || opts.Offset != nil) {
		return Options{}, fmt.Errorf("--limit and --offset only apply to `aicode session list`\n\n%s", listUsage)
	}
	return opts, nil
}

func flagTarget(opts *Options, name string) (**int, bool) {
	switch name {
	case "--limit":
		return &opts.Limit, true
	case "--offset":
		return &opts.Offset, true
	case "--max-sessions":
		return &opts.MaxSessions, true
	case "--max-age-days":
		return &opts.MaxAgeDays, true
	}
	return nil, false
}
