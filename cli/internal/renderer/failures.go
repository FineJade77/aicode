package renderer

import (
	"fmt"
	"io"
	"sort"
	"strings"
)

// RunTracker collects the things that went wrong during a run so the end of the
// transcript can say what happened.
//
// The events are already printed individually, but they are printed *as they
// happen* — scattered through hundreds of lines of tool output that the user
// scrolled past. A run that stopped because it burned its step budget after
// three denied commands currently ends with a summary sentence written by the
// model, which is the one narrator with an interest in the answer. This is the
// mechanical account: what was refused, what failed, what ran out.
type RunTracker struct {
	toolErrors   map[string]int
	denied       map[string]int
	rejected     map[string]int
	editsApplied int
	editsRefused int
	budget       string
	verification string
	noProgress   bool
	serverFailed []string
}

func NewRunTracker() *RunTracker {
	return &RunTracker{
		toolErrors: map[string]int{},
		denied:     map[string]int{},
		rejected:   map[string]int{},
	}
}

// Observe records one event. Unknown events are ignored, so a future event type
// cannot break the summary.
func (t *RunTracker) Observe(event map[string]any) {
	switch stringValue(event["type"]) {
	case "tool.error":
		t.toolErrors[stringValue(event["tool"])]++
	case "tool.denied":
		t.denied[stringValue(event["tool"])]++
	case "tool.rejected":
		t.rejected[stringValue(event["tool"])]++
	case "edit.applied", "edit.auto_approved":
		t.editsApplied++
	case "edit.rejected":
		t.editsRefused++
	case "run.budget.exceeded":
		t.budget = stringValue(event["reason"])
	case "run.verification.exhausted":
		t.verification = stringValue(event["message"])
	case "run.no_progress":
		t.noProgress = true
	case "mcp.server.failed":
		t.serverFailed = append(t.serverFailed, stringValue(event["server"]))
	}
}

// Summary returns the closing account, or "" when nothing went wrong.
//
// Silence on a clean run is deliberate: a summary that always prints teaches the
// reader to skip it, and then it is not there when it matters.
func (t *RunTracker) Summary() string {
	var reasons []string
	if t.budget != "" {
		reasons = append(reasons, fmt.Sprintf("stopped on the %s budget", t.budget))
	}
	if t.verification != "" {
		reasons = append(reasons, "verification never passed")
	}
	if t.noProgress {
		reasons = append(reasons, "the same action repeated without progress")
	}
	if count := total(t.denied); count > 0 {
		reasons = append(reasons, fmt.Sprintf("%d command(s) denied by policy: %s", count, names(t.denied)))
	}
	if count := total(t.rejected); count > 0 {
		reasons = append(reasons, fmt.Sprintf("%d tool call(s) not approved: %s", count, names(t.rejected)))
	}
	if count := total(t.toolErrors); count > 0 {
		reasons = append(reasons, fmt.Sprintf("%d tool failure(s): %s", count, names(t.toolErrors)))
	}
	if t.editsRefused > 0 {
		reasons = append(reasons, fmt.Sprintf("%d edit(s) not applied", t.editsRefused))
	}
	if len(t.serverFailed) > 0 {
		reasons = append(reasons, fmt.Sprintf("MCP server(s) unavailable: %s", strings.Join(t.serverFailed, ", ")))
	}
	if len(reasons) == 0 {
		return ""
	}
	var out strings.Builder
	out.WriteString("\nWhat went wrong:\n")
	for _, reason := range reasons {
		out.WriteString("  - " + reason + "\n")
	}
	if t.editsApplied > 0 {
		// Stated because it changes what to do next: a run that failed *after*
		// writing files leaves the workspace modified.
		out.WriteString(fmt.Sprintf("  (%d edit(s) were applied before this)\n", t.editsApplied))
	}
	return out.String()
}

// PrintSummaryTo writes the closing account, if there is one.
func (t *RunTracker) PrintSummaryTo(out io.Writer) {
	if summary := t.Summary(); summary != "" {
		fmt.Fprint(out, summary)
	}
}

func total(counts map[string]int) int {
	sum := 0
	for _, count := range counts {
		sum += count
	}
	return sum
}

func names(counts map[string]int) string {
	keys := make([]string, 0, len(counts))
	for key := range counts {
		if key == "" {
			key = "unknown"
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return strings.Join(keys, ", ")
}
