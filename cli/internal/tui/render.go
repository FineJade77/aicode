package tui

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// ANSI attributes. Written out rather than pulled from a styling library for the
// same reason the whole package avoids one.
const (
	reset   = "\x1b[0m"
	dim     = "\x1b[2m"
	bold    = "\x1b[1m"
	red     = "\x1b[31m"
	green   = "\x1b[32m"
	yellow  = "\x1b[33m"
	blue    = "\x1b[34m"
	magenta = "\x1b[35m"
	cyan    = "\x1b[36m"
)

// MinHeight is the smallest window the layout can honour: header, budget, one
// transcript line, input and status. Below it the view degrades to a message
// rather than drawing panes that overlap.
const MinHeight = 6

// Render returns exactly `height` lines of exactly `width` visible columns.
//
// Pure, so the layout can be tested character by character without a terminal.
// The caller writes them; nothing here touches a file descriptor.
func (m *Model) Render(width int, height int) []string {
	if width < 20 || height < MinHeight {
		// Still exactly `height` rows: returning fewer scrolls the screen on
		// every repaint, which reads as a broken terminal rather than a window
		// that needs resizing.
		rows := make([]string, 0, max(height, 1))
		rows = append(rows, pad(clip("aicode: window too small", width), width))
		for len(rows) < height {
			rows = append(rows, strings.Repeat(" ", max(width, 0)))
		}
		return rows
	}
	out := make([]string, 0, height)
	out = append(out, m.header(width))
	out = append(out, m.budgetBar(width))

	plan := m.planLines(width)
	// Fixed rows: header, budget, input, status.
	transcriptHeight := height - 4 - len(plan)
	if transcriptHeight < 1 {
		// The plan is the first thing to go: it is a summary of work the
		// transcript already describes, so losing it costs less than losing the
		// conversation.
		plan = nil
		transcriptHeight = height - 4
	}
	out = append(out, m.transcript(width, transcriptHeight)...)
	out = append(out, plan...)
	out = append(out, m.inputLine(width))
	out = append(out, m.statusLine(width))
	return out
}

func (m *Model) header(width int) string {
	session := m.SessionID
	if session == "" {
		session = "(new)"
	}
	model := m.ModelName
	if model == "" {
		model = m.Budget.Model
	}
	if model == "" {
		model = "route:main"
	}
	left := fmt.Sprintf(" aicode  %s  %s  sandbox:%s", session, model, m.Sandbox)
	return bold + pad(clip(left, width), width) + reset
}

// budgetBar draws usage against the *usable* budget, not the raw window.
//
// The reserve is held back for the reply, so a bar drawn against the window
// would show headroom that does not exist — the same reason the snapshot
// reports `usable_tokens`.
func (m *Model) budgetBar(width int) string {
	if m.Budget.UsableTokens <= 0 {
		return dim + pad(clip(" context: unknown", width), width) + reset
	}
	label := fmt.Sprintf(
		" context %s/%s  %d%%",
		compactNumber(m.Budget.UsedTokens),
		compactNumber(m.Budget.UsableTokens),
		int(m.Budget.UsedRatio*100+0.5),
	)
	if m.Budget.CompactionDue {
		label += "  (compaction due)"
	}
	barWidth := width - utf8.RuneCountInString(label) - 3
	if barWidth < 4 {
		return dim + pad(clip(label, width), width) + reset
	}
	filled := int(m.Budget.UsedRatio * float64(barWidth))
	if filled > barWidth {
		filled = barWidth
	}
	if filled < 0 {
		filled = 0
	}
	colour := green
	switch {
	case m.Budget.UsedRatio >= 0.9:
		colour = red
	case m.Budget.UsedRatio >= 0.7:
		colour = yellow
	}
	bar := colour + strings.Repeat("█", filled) + reset + dim + strings.Repeat("░", barWidth-filled) + reset
	return pad(clip(label, width-barWidth-2), width-barWidth-2) + " " + bar + " "
}

func (m *Model) transcript(width int, height int) []string {
	wrapped := make([]Line, 0, len(m.Lines))
	for _, line := range m.Lines {
		for _, piece := range wrap(line.Text, width-1) {
			wrapped = append(wrapped, Line{Kind: line.Kind, Text: piece})
		}
	}
	end := len(wrapped) - m.Scroll
	if end < 0 {
		end = 0
	}
	start := end - height
	if start < 0 {
		start = 0
	}
	rows := make([]string, 0, height)
	for _, line := range wrapped[start:end] {
		rows = append(rows, colourFor(line.Kind)+pad(clip(" "+line.Text, width), width)+reset)
	}
	for len(rows) < height {
		// Blank rows at the top keep the conversation anchored to the input,
		// which is where the eye is between turns.
		rows = append([]string{strings.Repeat(" ", width)}, rows...)
	}
	return rows
}

func (m *Model) planLines(width int) []string {
	if len(m.Plan) == 0 {
		return nil
	}
	rows := []string{dim + pad(clip(" plan", width), width) + reset}
	for _, item := range m.Plan {
		marker, colour := "·", dim
		switch item.Status {
		case "done":
			marker, colour = "✓", green
		case "in_progress":
			marker, colour = "▸", cyan
		}
		rows = append(rows, colour+pad(clip(fmt.Sprintf(" %s %s", marker, item.Text), width), width)+reset)
	}
	return rows
}

func (m *Model) inputLine(width int) string {
	if m.Pending != nil {
		return magenta + pad(clip(" "+approvalPrompt(m.Pending), width), width) + reset
	}
	return pad(clip("> "+m.Input, width), width)
}

func approvalPrompt(approval *Approval) string {
	switch approval.Kind {
	case "question":
		return approval.Prompt + "  [type an answer, or /skip]"
	case "edit":
		if len(approval.Paths) > 1 {
			return fmt.Sprintf("apply %d edits? [y/a/n or 1,3 for a subset]", len(approval.Paths))
		}
		return "apply this edit? [y=apply / a=apply and allow later / n=deny]"
	default:
		return "allow this operation? [y/n]"
	}
}

func (m *Model) statusLine(width int) string {
	state := m.Status
	if m.Running {
		state = "● " + state
	}
	hint := "^C cancel · ^D quit · PgUp/PgDn scroll"
	if m.Scroll > 0 {
		hint = fmt.Sprintf("scrolled %d · End to follow", m.Scroll)
	}
	gap := width - utf8.RuneCountInString(state) - utf8.RuneCountInString(hint) - 2
	if gap < 1 {
		return dim + pad(clip(" "+state, width), width) + reset
	}
	return dim + " " + state + strings.Repeat(" ", gap) + hint + " " + reset
}

func colourFor(kind string) string {
	switch kind {
	case "user":
		return blue
	case "error":
		return red
	case "tool":
		return cyan
	case "event":
		return dim
	case "system":
		return yellow
	default:
		return ""
	}
}

// clip truncates to a rune count, so a multi-byte character is never cut in half
// and left as a broken glyph on screen.
func clip(value string, width int) string {
	if width <= 0 {
		return ""
	}
	if utf8.RuneCountInString(value) <= width {
		return value
	}
	runes := []rune(value)
	if width == 1 {
		return "…"
	}
	return string(runes[:width-1]) + "…"
}

func pad(value string, width int) string {
	missing := width - utf8.RuneCountInString(value)
	if missing <= 0 {
		return value
	}
	return value + strings.Repeat(" ", missing)
}

func wrap(value string, width int) []string {
	if width <= 0 {
		return []string{""}
	}
	runes := []rune(value)
	if len(runes) == 0 {
		return []string{""}
	}
	out := make([]string, 0, len(runes)/width+1)
	for len(runes) > width {
		out = append(out, string(runes[:width]))
		runes = runes[width:]
	}
	return append(out, string(runes))
}

func compactNumber(value int) string {
	switch {
	case value >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(value)/1_000_000)
	case value >= 1_000:
		return fmt.Sprintf("%.1fk", float64(value)/1_000)
	default:
		return fmt.Sprintf("%d", value)
	}
}
