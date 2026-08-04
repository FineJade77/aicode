package tui

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func visible(row string) string {
	var out strings.Builder
	for index := 0; index < len(row); {
		if row[index] == 0x1b {
			for index < len(row) && row[index] != 'm' {
				index++
			}
			index++
			continue
		}
		out.WriteByte(row[index])
		index++
	}
	return out.String()
}

func TestRenderFillsExactlyTheWindow(t *testing.T) {
	// A pane that returns the wrong number of rows scrolls the screen on every
	// repaint, which looks like the terminal is broken rather than the layout.
	model := New("sess_1", "/repo")
	for index := range 50 {
		model.Apply(map[string]any{"type": "assistant.delta", "text": "line\n"})
		_ = index
	}

	for _, size := range [][2]int{{80, 24}, {120, 40}, {60, MinHeight}} {
		rows := model.Render(size[0], size[1])
		if len(rows) != size[1] {
			t.Fatalf("%v: got %d rows", size, len(rows))
		}
		for _, row := range rows {
			if width := utf8.RuneCountInString(visible(row)); width != size[0] {
				t.Fatalf("%v: row %q is %d columns", size, visible(row), width)
			}
		}
	}
}

func TestATinyWindowDegradesInsteadOfOverlapping(t *testing.T) {
	// Still exactly the window's height: a short frame scrolls the screen on
	// every repaint, which looks like a broken terminal rather than a small one.
	rows := New("s", "/repo").Render(30, 3)

	if len(rows) != 3 {
		t.Fatalf("rows = %#v", rows)
	}
	if !strings.Contains(rows[0], "too small") {
		t.Fatalf("rows = %#v", rows)
	}
	for _, row := range rows {
		if utf8.RuneCountInString(visible(row)) != 30 {
			t.Fatalf("row %q", visible(row))
		}
	}
}

func TestStreamedDeltasBecomeOneLine(t *testing.T) {
	// One line per token is unreadable and makes scrollback useless.
	model := New("s", "/repo")

	model.Apply(map[string]any{"type": "assistant.delta", "text": "Hel"})
	model.Apply(map[string]any{"type": "assistant.delta", "text": "lo"})

	if len(model.Lines) != 1 || model.Lines[0].Text != "Hello" {
		t.Fatalf("lines = %#v", model.Lines)
	}
}

func TestAToolCallBreaksTheStreamingLine(t *testing.T) {
	model := New("s", "/repo")
	model.Apply(map[string]any{"type": "assistant.delta", "text": "thinking"})
	model.Apply(map[string]any{"type": "tool.started", "tool": "bash"})
	model.Apply(map[string]any{"type": "assistant.delta", "text": "done"})

	if len(model.Lines) != 3 {
		t.Fatalf("lines = %#v", model.Lines)
	}
	if model.Lines[2].Text != "done" {
		t.Fatalf("the second answer must not append to the first: %#v", model.Lines)
	}
}

func TestLongToolOutputIsFoldedAndSaysSo(t *testing.T) {
	model := New("s", "/repo")
	model.Apply(map[string]any{"type": "tool.output", "text": strings.Repeat("x\n", 30)})

	joined := ""
	for _, line := range model.Lines {
		joined += line.Text
	}
	if !strings.Contains(joined, "more lines folded") {
		t.Fatalf("lines = %#v", model.Lines)
	}
	// The reader must not conclude the model only saw the head.
	if !strings.Contains(joined, "the model received all of it") {
		t.Fatalf("lines = %#v", model.Lines)
	}
}

func TestTheTranscriptIsBounded(t *testing.T) {
	model := New("s", "/repo")
	for range MaxLines + 500 {
		model.Apply(map[string]any{"type": "tool.started", "tool": "bash"})
	}

	if len(model.Lines) != MaxLines {
		t.Fatalf("lines = %d", len(model.Lines))
	}
}

func TestScrollingBackSurvivesNewOutput(t *testing.T) {
	// Yanking the view to the bottom mid-read is the classic way a log pane
	// becomes unusable exactly when something interesting is happening.
	model := New("s", "/repo")
	for range 40 {
		model.Apply(map[string]any{"type": "tool.started", "tool": "bash"})
	}
	model.Scroll = 10

	model.Apply(map[string]any{"type": "tool.started", "tool": "ls"})

	if model.Scroll != 11 {
		t.Fatalf("scroll = %d", model.Scroll)
	}
}

func TestTheBudgetBarIsDrawnAgainstTheUsableBudget(t *testing.T) {
	// Not the raw window: the reserve is held back for the reply, so a bar drawn
	// against the window advertises headroom that does not exist.
	model := New("s", "/repo")
	model.ApplyStatus("claude-sonnet-5", "host", Context{
		ContextWindow: 200000, UsableTokens: 100000, UsedTokens: 50000, UsedRatio: 0.5,
	})

	bar := visible(model.Render(100, 24)[1])

	if !strings.Contains(bar, "50.0k/100.0k") || !strings.Contains(bar, "50%") {
		t.Fatalf("bar = %q", bar)
	}
}

func TestAnUnknownBudgetSaysUnknown(t *testing.T) {
	bar := visible(New("s", "/repo").Render(100, 24)[1])

	if !strings.Contains(bar, "unknown") {
		t.Fatalf("bar = %q", bar)
	}
}

func TestApprovalTakesOverTheInputLine(t *testing.T) {
	model := New("s", "/repo")
	model.Apply(map[string]any{
		"type": "approval.requested", "approval_id": "a1", "kind": "edit",
		"paths": []any{"a.py", "b.py"},
	})

	rows := model.Render(80, 24)
	prompt := visible(rows[len(rows)-2])

	if !strings.Contains(prompt, "apply 2 edits?") {
		t.Fatalf("prompt = %q", prompt)
	}
}

func TestAQuestionIsNotParsedAsYesOrNo(t *testing.T) {
	model := New("s", "/repo")
	model.Apply(map[string]any{"type": "question.asked", "approval_id": "q1", "question": "Which one?"})

	rows := model.Render(80, 24)
	prompt := visible(rows[len(rows)-2])

	if !strings.Contains(prompt, "type an answer") {
		t.Fatalf("prompt = %q", prompt)
	}
}

func TestFinalClearsTheRunState(t *testing.T) {
	model := New("s", "/repo")
	model.Apply(map[string]any{"type": "run.started"})
	model.Apply(map[string]any{"type": "approval.requested", "approval_id": "a1", "kind": "edit"})

	model.Apply(map[string]any{"type": "final", "summary": "done"})

	if model.Running || model.Pending != nil {
		t.Fatalf("model = %#v", model)
	}
}

func TestUnknownEventsAreIgnored(t *testing.T) {
	model := New("s", "/repo")

	model.Apply(map[string]any{"type": "some.future.event", "detail": "whatever"})

	if len(model.Lines) != 0 {
		t.Fatalf("lines = %#v", model.Lines)
	}
}

func TestPlanRendersWithStatusMarkers(t *testing.T) {
	model := New("s", "/repo")
	model.Apply(map[string]any{"type": "plan.updated", "items": []any{
		map[string]any{"text": "read", "status": "done"},
		map[string]any{"text": "edit", "status": "in_progress"},
	}})

	joined := strings.Join(model.Render(80, 24), "\n")

	if !strings.Contains(joined, "✓ read") || !strings.Contains(joined, "▸ edit") {
		t.Fatalf("render = %q", visible(joined))
	}
}

func TestWideCharactersAreClippedByRuneNotByte(t *testing.T) {
	// Cutting a multi-byte rune in half leaves a broken glyph on screen.
	model := New("s", "/repo")
	model.AppendUser(strings.Repeat("界", 200))

	for _, row := range model.Render(40, 24) {
		if !utf8.ValidString(visible(row)) {
			t.Fatalf("row is not valid UTF-8: %q", row)
		}
	}
}

func TestSelectedPathsRefusesAnOutOfRangeIndex(t *testing.T) {
	paths := []string{"a.py", "b.py"}

	if got := selectedPaths("1,2", paths); len(got) != 2 {
		t.Fatalf("got = %#v", got)
	}
	for _, answer := range []string{"3", "0", "1,9", "x"} {
		if got := selectedPaths(answer, paths); got != nil {
			t.Fatalf("answer %q selected %#v", answer, got)
		}
	}
}
