package tui

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/FineJade77/aicode/cli/internal/client"
)

const requestTimeout = 10 * time.Second

// API is the Runtime surface the view needs. Narrow on purpose: the view is a
// renderer, and a wide interface here would invite it to become a second CLI.
type API interface {
	CreateSession(context.Context, client.CreateSessionRequest) (client.CreateSessionResponse, error)
	GetSession(context.Context, string) (client.SessionResponse, error)
	SendMessage(context.Context, string, client.SendMessageRequest) (client.SendMessageResponse, error)
	StreamRunEvents(context.Context, string, string, func(map[string]any) error) error
	CancelRun(context.Context, string) (client.CancelRunResponse, error)
	Approve(context.Context, string, string, bool) error
	ApproveSelection(context.Context, string, string, bool, []string) error
	Reject(context.Context, string, string) error
	Answer(context.Context, string, string, string) error
}

type key struct {
	rune rune
	name string // "enter" | "backspace" | "ctrl-c" | "ctrl-d" | "pgup" | "pgdn" | "end" | ""
}

// Session runs the view until the user quits.
type Session struct {
	API       API
	Terminal  *Terminal
	Workspace string
	Out       *os.File

	model *Model
}

// Run owns the whole lifetime: it creates the session, starts the input reader,
// and redraws on every state change.
func (s *Session) Run(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	created, err := s.create(ctx)
	if err != nil {
		return err
	}
	s.model = New(created, s.Workspace)
	s.refreshStatus(ctx)

	keys := make(chan key, 32)
	go readKeys(ctx, os.Stdin, keys)
	events := make(chan map[string]any, 256)
	done := make(chan struct{}, 8)

	s.draw()
	for {
		select {
		case <-ctx.Done():
			return nil
		case event := <-events:
			s.model.Apply(event)
			if text(event["type"]) == "final" {
				s.refreshStatus(ctx)
			}
			s.draw()
		case <-done:
			s.model.Running = false
			s.refreshStatus(ctx)
			s.draw()
		case pressed := <-keys:
			if s.handleKey(ctx, pressed, events, done) {
				return nil
			}
			s.draw()
		}
	}
}

func (s *Session) create(ctx context.Context) (string, error) {
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	created, err := s.API.CreateSession(requestCtx, client.CreateSessionRequest{Workspace: s.Workspace})
	if err != nil {
		return "", err
	}
	return created.SessionID, nil
}

// handleKey returns true when the view should exit.
func (s *Session) handleKey(ctx context.Context, pressed key, events chan<- map[string]any, done chan<- struct{}) bool {
	switch pressed.name {
	case "ctrl-d":
		return true
	case "ctrl-c":
		// Cancel a run if one is active; otherwise quit. Two meanings for one
		// key, but they are never both available, so there is nothing to guess.
		if !s.model.Running {
			return true
		}
		go s.cancel(ctx)
		s.model.Status = "cancelling"
		return false
	case "pgup":
		s.model.Scroll += 10
		return false
	case "pgdn":
		s.model.Scroll -= 10
		if s.model.Scroll < 0 {
			s.model.Scroll = 0
		}
		return false
	case "end":
		s.model.Scroll = 0
		return false
	case "backspace":
		if runes := []rune(s.model.Input); len(runes) > 0 {
			s.model.Input = string(runes[:len(runes)-1])
		}
		return false
	case "enter":
		line := strings.TrimSpace(s.model.Input)
		s.model.Input = ""
		if line == "" {
			return false
		}
		if s.model.Pending != nil {
			s.resolvePending(ctx, line)
			return false
		}
		s.model.AppendUser(line)
		s.model.Scroll = 0
		go s.submit(ctx, line, events, done)
		return false
	}
	if pressed.rune != 0 {
		s.model.Input += string(pressed.rune)
	}
	return false
}

// resolvePending answers whatever the run is blocked on.
//
// A question takes free text, so it must be checked before the y/n parser:
// "no" is an answer to a question, not a refusal of it.
func (s *Session) resolvePending(ctx context.Context, line string) {
	pending := s.model.Pending
	s.model.Pending = nil
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()

	if pending.Kind == "question" {
		if line == "/skip" {
			_ = s.API.Reject(requestCtx, s.model.SessionID, pending.ID)
			return
		}
		_ = s.API.Answer(requestCtx, s.model.SessionID, pending.ID, line)
		return
	}
	switch strings.ToLower(line) {
	case "y", "yes":
		_ = s.API.Approve(requestCtx, s.model.SessionID, pending.ID, false)
	case "a", "all":
		_ = s.API.Approve(requestCtx, s.model.SessionID, pending.ID, true)
	default:
		if selection := selectedPaths(line, pending.Paths); len(selection) > 0 {
			_ = s.API.ApproveSelection(requestCtx, s.model.SessionID, pending.ID, false, selection)
			return
		}
		_ = s.API.Reject(requestCtx, s.model.SessionID, pending.ID)
	}
}

// selectedPaths maps "1,3" onto those entries. An index outside the list voids
// the whole selection rather than shifting the rest, because silently applying a
// different set than the user named is worse than making them retype.
func selectedPaths(answer string, paths []string) []string {
	if len(paths) == 0 {
		return nil
	}
	var selection []string
	for _, field := range strings.FieldsFunc(answer, func(r rune) bool { return r == ',' || r == ' ' }) {
		index, err := strconv.Atoi(strings.TrimSpace(field))
		if err != nil || index < 1 || index > len(paths) {
			return nil
		}
		selection = append(selection, paths[index-1])
	}
	return selection
}

func (s *Session) submit(ctx context.Context, message string, events chan<- map[string]any, done chan<- struct{}) {
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	run, err := s.API.SendMessage(requestCtx, s.model.SessionID, client.SendMessageRequest{
		Message:     message,
		Mode:        "chat",
		Workspace:   s.Workspace,
		BashBackend: sandboxOverride(s.model.Sandbox),
	})
	cancel()
	if err != nil {
		events <- map[string]any{"type": "error", "error": err.Error()}
		done <- struct{}{}
		return
	}
	streamErr := s.API.StreamRunEvents(ctx, s.model.SessionID, run.RunID, func(event map[string]any) error {
		events <- event
		return nil
	})
	if streamErr != nil && ctx.Err() == nil {
		events <- map[string]any{"type": "error", "error": streamErr.Error()}
	}
	done <- struct{}{}
}

func sandboxOverride(value string) string {
	switch value {
	case "auto", "host", "docker", "os":
		return value
	default:
		return ""
	}
}

func (s *Session) cancel(ctx context.Context) {
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	_, _ = s.API.CancelRun(requestCtx, s.model.SessionID)
}

// refreshStatus pulls the model and context budget from the session snapshot.
//
// Read from the Runtime rather than accumulated locally: the snapshot computes
// usage with the same estimator the preflight uses, and a second local estimate
// would drift into showing a different number than the one that triggers
// compaction.
func (s *Session) refreshStatus(ctx context.Context) {
	requestCtx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	status, err := s.API.GetSession(requestCtx, s.model.SessionID)
	if err != nil || status.Context == nil {
		return
	}
	s.model.ApplyStatus(status.Context.Model, "", Context{
		Provider:      status.Context.Provider,
		Model:         status.Context.Model,
		ContextWindow: status.Context.ContextWindow,
		UsableTokens:  status.Context.UsableTokens,
		UsedTokens:    status.Context.UsedTokens,
		UsedRatio:     status.Context.UsedRatio,
		CompactionDue: status.Context.CompactionDue,
	})
}

func (s *Session) draw() {
	width, height := s.Terminal.Size()
	rows := s.model.Render(width, height)
	var screen strings.Builder
	// Home the cursor and repaint every row rather than clearing first: clearing
	// makes the whole screen flash on terminals that do not batch the update.
	screen.WriteString("\x1b[H")
	for index, row := range rows {
		screen.WriteString("\x1b[K")
		screen.WriteString(row)
		if index < len(rows)-1 {
			screen.WriteString("\r\n")
		}
	}
	fmt.Fprint(s.Out, screen.String())
}

// readKeys decodes stdin into key events, including the escape sequences for
// the navigation keys the view uses.
func readKeys(ctx context.Context, input *os.File, out chan<- key) {
	reader := bufio.NewReader(input)
	for {
		if ctx.Err() != nil {
			return
		}
		value, _, err := reader.ReadRune()
		if err != nil {
			return
		}
		switch value {
		case '\r', '\n':
			send(ctx, out, key{name: "enter"})
		case 127, 8:
			send(ctx, out, key{name: "backspace"})
		case 3:
			send(ctx, out, key{name: "ctrl-c"})
		case 4:
			send(ctx, out, key{name: "ctrl-d"})
		case 27:
			send(ctx, out, decodeEscape(reader))
		default:
			if value >= 32 {
				send(ctx, out, key{rune: value})
			}
		}
	}
}

// decodeEscape reads the remainder of a CSI sequence.
//
// Unrecognised sequences resolve to an empty key rather than being typed into
// the input: an arrow key that inserts "[D" is worse than one that does nothing.
func decodeEscape(reader *bufio.Reader) key {
	next, _, err := reader.ReadRune()
	if err != nil || next != '[' {
		return key{}
	}
	sequence := make([]rune, 0, 4)
	for range 4 {
		value, _, err := reader.ReadRune()
		if err != nil {
			return key{}
		}
		sequence = append(sequence, value)
		if value >= '@' && value <= '~' {
			break
		}
	}
	switch string(sequence) {
	case "5~":
		return key{name: "pgup"}
	case "6~":
		return key{name: "pgdn"}
	case "F":
		return key{name: "end"}
	}
	return key{}
}

func send(ctx context.Context, out chan<- key, pressed key) {
	if pressed.rune == 0 && pressed.name == "" {
		return
	}
	select {
	case out <- pressed:
	case <-ctx.Done():
	}
}
