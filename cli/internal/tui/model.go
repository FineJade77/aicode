// Package tui renders the full-screen aicode session view.
//
// Built on the standard library alone. The CLI has no third-party dependencies
// and a terminal UI is not a good enough reason to acquire the first one: it
// would pull a framework, a module graph and a lockfile into a binary whose
// current appeal is that `go build` needs nothing. The cost is that raw mode is
// implemented per platform here, and Windows is unsupported.
//
// The state and the drawing are pure functions over an event stream. Everything
// that talks to a terminal or a socket lives in run.go and term_*.go, so the
// parts worth testing can be tested without either.
package tui

import (
	"fmt"
	"strings"
)

// Line is one rendered transcript entry, tagged so the view can style it.
type Line struct {
	Kind string // "user" | "agent" | "tool" | "event" | "error" | "system"
	Text string
}

// Context mirrors the session snapshot's context block.
type Context struct {
	Provider      string
	Model         string
	ContextWindow int
	UsableTokens  int
	UsedTokens    int
	UsedRatio     float64
	CompactionDue bool
}

// PlanItem is one entry of the agent's declared plan.
type PlanItem struct {
	Text   string
	Status string
}

// Approval is a decision the run is blocked on.
type Approval struct {
	ID     string
	Kind   string
	Prompt string
	Paths  []string
}

// Model is the whole view state. Deliberately plain data: every transition is a
// method that takes an event and returns nothing, so a test can drive the exact
// sequence a session would produce.
type Model struct {
	SessionID string
	Workspace string
	Sandbox   string
	ModelName string

	Lines  []Line
	Plan   []PlanItem
	Budget Context

	Input    string
	Scroll   int // lines scrolled back from the bottom; 0 follows the tail
	Running  bool
	Status   string
	Pending  *Approval
	Quitting bool

	// Streamed assistant text accumulates into one line rather than one per
	// delta; a transcript of single tokens is unreadable and unscrollable.
	streaming bool
}

// MaxLines bounds transcript memory. A long session must not grow the process
// without limit, and nothing above the cap is reachable by scrolling anyway.
const MaxLines = 5000

func New(sessionID string, workspace string) *Model {
	return &Model{
		SessionID: sessionID,
		Workspace: workspace,
		Sandbox:   "default",
		Status:    "ready",
	}
}

// AppendUser records a submitted message.
func (m *Model) AppendUser(text string) {
	m.append(Line{Kind: "user", Text: text})
	m.streaming = false
}

// Apply folds one SSE event into the view.
//
// Unknown event types are ignored rather than rendered raw: a future event has
// no place in a fixed-height pane, and the CLI's line renderer already exists
// for the case where seeing everything matters.
func (m *Model) Apply(event map[string]any) {
	switch text(event["type"]) {
	case "run.started":
		m.Running = true
		m.Status = "running"
	case "assistant.delta":
		m.appendStream(text(event["text"]))
	case "tool.started":
		m.streaming = false
		m.append(Line{Kind: "tool", Text: "· " + text(event["tool"])})
		m.Status = "tool: " + text(event["tool"])
	case "tool.output":
		m.streaming = false
		if body := strings.TrimSpace(text(event["text"])); body != "" {
			m.appendFolded(body)
		}
	case "tool.error", "error":
		m.streaming = false
		m.append(Line{Kind: "error", Text: "! " + text(event["error"])})
	case "tool.denied":
		m.streaming = false
		m.append(Line{Kind: "error", Text: "! denied: " + text(event["tool"]) + " (" + text(event["error"]) + ")"})
	case "edit.applied":
		m.streaming = false
		m.append(Line{Kind: "event", Text: "+ edit applied: " + text(event["path"])})
	case "edit.rejected":
		m.streaming = false
		m.append(Line{Kind: "event", Text: "- edit not applied: " + text(event["path"])})
	case "plan.updated":
		m.Plan = planFrom(event["items"])
	case "context.budget":
		m.Budget.ContextWindow = number(event["context_window"])
		if model := text(event["model"]); model != "" {
			m.Budget.Model = model
		}
		if after := number(event["after_tokens"]); after > 0 {
			m.Budget.UsedTokens = after
			if m.Budget.UsableTokens > 0 {
				m.Budget.UsedRatio = float64(after) / float64(m.Budget.UsableTokens)
			}
		}
	case "provider.fallback":
		m.append(Line{Kind: "event", Text: "~ " + text(event["message"])})
	case "mcp.server.failed":
		m.append(Line{Kind: "error", Text: "! MCP server " + text(event["server"]) + " unavailable"})
	case "approval.requested":
		m.Pending = &Approval{
			ID:     text(event["approval_id"]),
			Kind:   text(event["kind"]),
			Prompt: text(event["message"]),
			Paths:  stringList(event["paths"]),
		}
		m.Status = "approval required"
	case "question.asked":
		m.Pending = &Approval{ID: text(event["approval_id"]), Kind: "question", Prompt: text(event["question"])}
		m.Status = "question"
	case "approval.expired":
		m.Pending = nil
	case "run.cancelled":
		m.append(Line{Kind: "system", Text: text(event["message"])})
	case "final":
		m.streaming = false
		m.Running = false
		m.Pending = nil
		m.Status = "ready"
	}
}

// ApplyStatus folds a session snapshot into the header.
func (m *Model) ApplyStatus(modelName string, sandbox string, budget Context) {
	if modelName != "" {
		m.ModelName = modelName
	}
	if sandbox != "" {
		m.Sandbox = sandbox
	}
	if budget.ContextWindow > 0 {
		m.Budget = budget
	}
}

func (m *Model) appendStream(chunk string) {
	if chunk == "" {
		return
	}
	if m.streaming && len(m.Lines) > 0 {
		m.Lines[len(m.Lines)-1].Text += chunk
		return
	}
	m.streaming = true
	m.append(Line{Kind: "agent", Text: chunk})
}

// appendFolded keeps a bounded head of tool output.
//
// The same reasoning as the line renderer's fold: a `cat` of a large file would
// push the conversation out of a pane the user is reading, and the count says
// the model still received all of it.
func (m *Model) appendFolded(body string) {
	lines := strings.Split(body, "\n")
	const keep = 8
	if len(lines) <= keep {
		for _, line := range lines {
			m.append(Line{Kind: "event", Text: "  " + line})
		}
		return
	}
	for _, line := range lines[:keep] {
		m.append(Line{Kind: "event", Text: "  " + line})
	}
	m.append(Line{
		Kind: "system",
		Text: fmt.Sprintf("  … %d more lines folded; the model received all of it", len(lines)-keep),
	})
}

func (m *Model) append(line Line) {
	m.Lines = append(m.Lines, line)
	if len(m.Lines) > MaxLines {
		m.Lines = m.Lines[len(m.Lines)-MaxLines:]
	}
	// Appending while scrolled back would yank the view to the bottom mid-read.
	if m.Scroll > 0 {
		m.Scroll++
	}
}

func planFrom(value any) []PlanItem {
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	out := make([]PlanItem, 0, len(items))
	for _, item := range items {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		out = append(out, PlanItem{Text: text(entry["text"]), Status: text(entry["status"])})
	}
	return out
}

func text(value any) string {
	if value == nil {
		return ""
	}
	if s, ok := value.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", value)
}

func number(value any) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	}
	return 0
}

func stringList(value any) []string {
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}
