// Package chatcmd implements the persistent `aicode chat` session.
package chatcmd

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/renderer"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

const requestTimeout = 10 * time.Second

// API is the versioned Runtime surface used by the REPL.
type API interface {
	CreateSession(context.Context, client.CreateSessionRequest) (client.CreateSessionResponse, error)
	GetSession(context.Context, string) (client.SessionResponse, error)
	LastSession(context.Context) (client.SessionResponse, bool, error)
	SendMessage(context.Context, string, client.SendMessageRequest) (client.SendMessageResponse, error)
	StreamRunEvents(context.Context, string, string, func(map[string]any) error) error
	CancelRun(context.Context, string) (client.CancelRunResponse, error)
	Steer(context.Context, string, string) (client.SteerResponse, error)
	Compact(context.Context, string) (client.CompactResponse, error)
	Approve(context.Context, string, string, bool) error
	Reject(context.Context, string, string) error
	Answer(context.Context, string, string, string) error
	GetJSON(context.Context, string) (any, error)
}

// Runner owns the REPL state machine. Its injected ports keep TTY and piped
// behavior deterministic in tests.
type Runner struct {
	API         API
	In          io.Reader
	Out         io.Writer
	Err         io.Writer
	Workspace   string
	Interactive bool
	Signals     <-chan os.Signal

	// Session-scoped shell backend override, set by /sandbox. Empty leaves the
	// choice to the project and daemon configuration.
	sandbox         string
	outputMu        sync.Mutex
	previousSession *client.SessionResponse
	firstError      error
	tracker         *renderer.RunTracker
}

type inputLine struct {
	text string
	err  error
}

type runEvent struct {
	runID string
	value map[string]any
}

type streamResult struct {
	runID string
	err   error
}

type approvalPrompt struct {
	runID      string
	approvalID string
	kind       string
}

// Run starts the production REPL on the current workspace.
func Run(cfg config.Config) error {
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}
	root, err := workspace.Detect()
	if err != nil {
		return err
	}

	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)

	runner := Runner{
		API:         client.New(cfg.Runtime.URL, daemon.Token()),
		In:          os.Stdin,
		Out:         os.Stdout,
		Err:         os.Stderr,
		Workspace:   root.Path,
		Interactive: isTerminal(os.Stdin),
		Signals:     signals,
	}
	return runner.Run(context.Background())
}

// Run executes the persistent session loop until /exit, EOF after all queued
// runs, an idle interrupt, or a second interrupt during cancellation.
func (runner *Runner) Run(parent context.Context) error {
	if runner.API == nil {
		return errors.New("REPL API is required")
	}
	if runner.In == nil {
		runner.In = strings.NewReader("")
	}
	if runner.Out == nil {
		runner.Out = io.Discard
	}
	if runner.Err == nil {
		runner.Err = io.Discard
	}
	ctx, cancel := context.WithCancel(parent)
	defer cancel()

	previousCtx, previousCancel := withTimeout(ctx)
	previous, hasPrevious, previousErr := runner.API.LastSession(previousCtx)
	previousCancel()
	if previousErr == nil && hasPrevious {
		runner.previousSession = &previous
	}
	session, err := runner.createSession(ctx)
	if err != nil {
		return err
	}
	model := ""
	active := map[string]struct{}{}
	inputs := make(chan inputLine)
	events := make(chan runEvent, 64)
	streams := make(chan streamResult, 16)
	go scanInput(ctx, runner.In, inputs)

	runner.printf("Session: %s\n", session.SessionID)
	if runner.Interactive {
		runner.printf("Persistent REPL started. Enter /help to list commands.\n")
		runner.prompt(session.SessionID)
	}

	var pending *approvalPrompt
	inputClosed := false
	exitWhenIdle := false
	interruptArmed := false

	for {
		if inputClosed && len(active) == 0 {
			return runner.result()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case signalValue, ok := <-runner.Signals:
			if !ok {
				runner.Signals = nil
				continue
			}
			_ = signalValue
			if len(active) == 0 || interruptArmed {
				return runner.result()
			}
			interruptArmed = true
			runner.printf("\nCancelling the current run; press Ctrl-C again to exit.\n")
			go runner.cancelFromSignal(ctx, session.SessionID)
		case input := <-inputs:
			if input.err != nil {
				inputClosed = true
				exitWhenIdle = true
				if pending != nil {
					if err := runner.resolveApproval(ctx, session.SessionID, pending, "n"); err != nil {
						runner.printError(err)
					}
					pending = nil
				}
				continue
			}
			line := strings.TrimSpace(input.text)
			if line == "" {
				if runner.Interactive {
					runner.prompt(session.SessionID)
				}
				continue
			}
			interruptArmed = false
			if pending != nil && pending.kind == "question" && !strings.HasPrefix(line, "/") {
				// Any text is a valid answer, so this must be handled before the
				// y/n parser — "no" is an answer to a question, not a rejection.
				if err := runner.answerQuestion(ctx, session.SessionID, pending, line); err != nil {
					runner.printError(err)
				}
				pending = nil
				if runner.Interactive {
					runner.prompt(session.SessionID)
				}
				continue
			}
			if pending != nil && !strings.HasPrefix(line, "/") {
				if isApprovalAnswer(line, pending.kind) {
					if err := runner.resolveApproval(ctx, session.SessionID, pending, line); err != nil {
						runner.printError(err)
					}
					pending = nil
				} else {
					if err := runner.submit(ctx, session, model, line, active, events, streams); err != nil {
						runner.printError(err)
					}
					runner.printf("An approval is still pending; enter y, n%s, or use /cancel.\n", approvalAllHint(pending.kind))
				}
				if runner.Interactive {
					runner.prompt(session.SessionID)
				}
				continue
			}
			command, argument := splitCommand(line)
			if pending != nil && pending.kind == "question" && command == "/skip" {
				// Declining to answer, which the agent is told is not a refusal
				// of the underlying work.
				if err := runner.resolveApproval(ctx, session.SessionID, pending, "n"); err != nil {
					runner.printError(err)
				}
				pending = nil
				if runner.Interactive {
					runner.prompt(session.SessionID)
				}
				continue
			}
			if pending != nil && (command == "/approve" || command == "/reject") {
				answer := argument
				if command == "/reject" {
					answer = "n"
				} else if answer == "" {
					answer = "y"
				}
				if err := runner.resolveApproval(ctx, session.SessionID, pending, answer); err != nil {
					runner.printError(err)
				}
				pending = nil
				if runner.Interactive {
					runner.prompt(session.SessionID)
				}
				continue
			}
			if pending != nil && (command == "/steer" || command == "/exit" || command == "/quit") {
				if err := runner.resolveApproval(ctx, session.SessionID, pending, "n"); err != nil {
					runner.printError(err)
				}
				pending = nil
			}
			if strings.HasPrefix(line, "/") {
				shouldExit, updated, updatedModel := runner.handleCommand(
					ctx, line, session, model, active, events, streams,
				)
				session = updated
				model = updatedModel
				if shouldExit {
					if len(active) == 0 {
						return runner.result()
					}
					exitWhenIdle = true
					inputClosed = true
				}
			} else {
				if err := runner.submit(ctx, session, model, line, active, events, streams); err != nil {
					runner.printError(err)
				}
			}
			if runner.Interactive && !inputClosed {
				runner.prompt(session.SessionID)
			}
		case event := <-events:
			runner.renderEvent(event.value)
			if stringValue(event.value["type"]) == "question.asked" {
				approvalID := stringValue(event.value["approval_id"])
				if approvalID == "" {
					runner.printError(errors.New("question.asked is missing approval_id"))
					continue
				}
				runner.printf("Your answer (or /skip to let the agent decide): ")
				pending = &approvalPrompt{runID: event.runID, approvalID: approvalID, kind: "question"}
				continue
			}
			if stringValue(event.value["type"]) == "approval.requested" {
				approvalID := stringValue(event.value["approval_id"])
				if approvalID == "" {
					runner.printError(errors.New("approval.requested is missing approval_id"))
					continue
				}
				kind := stringValue(event.value["kind"])
				if kind == "edit" {
					if diff := stringValue(event.value["diff"]); diff != "" {
						runner.printf("%s\n", diff)
					}
					runner.printf("Apply this edit? [y=apply / a=apply and allow later edits in this session / n=deny]: ")
				} else {
					runner.printf("Allow this tool operation? [y=allow / n=deny]: ")
				}
				pending = &approvalPrompt{runID: event.runID, approvalID: approvalID, kind: kind}
			}
		case result := <-streams:
			delete(active, result.runID)
			if pending != nil && pending.runID == result.runID {
				pending = nil
			}
			if result.err != nil && !errors.Is(result.err, context.Canceled) {
				runner.printError(fmt.Errorf("run %s event stream: %w", result.runID, result.err))
			}
			if len(active) == 0 {
				interruptArmed = false
			}
			if exitWhenIdle && len(active) == 0 {
				return runner.result()
			}
			if runner.Interactive && !inputClosed {
				runner.prompt(session.SessionID)
			}
		}
	}
}

func (runner *Runner) handleCommand(
	ctx context.Context,
	line string,
	session client.SessionResponse,
	model string,
	active map[string]struct{},
	events chan<- runEvent,
	streams chan<- streamResult,
) (bool, client.SessionResponse, string) {
	command, argument := splitCommand(line)
	switch command {
	case "/help":
		runner.printf("%s", helpText)
	case "/exit", "/quit":
		return true, session, model
	case "/status":
		requestCtx, cancel := withTimeout(ctx)
		value, err := runner.API.GetSession(requestCtx, session.SessionID)
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		session = value
		runner.printStatus(session, model)
	case "/model":
		if argument != "" {
			model = argument
			runner.printf("Current message model override: %s\n", model)
			break
		}
		requestCtx, cancel := withTimeout(ctx)
		value, err := runner.API.GetJSON(requestCtx, "/v1/models/routes")
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		if model != "" {
			runner.printf("Current message model override: %s\n\n", model)
		}
		runner.printf("%s", renderer.ModelRoutesTable(value))
	case "/sandbox":
		// Session-scoped so one risky command can be sandboxed without
		// reconfiguring the daemon, and without the choice outliving the
		// session that made it.
		switch argument {
		case "":
			current := runner.sandbox
			if current == "" {
				current = "(project/daemon default)"
			}
			runner.printf("Shell backend for this session: %s\n", current)
			runner.printf("Usage: /sandbox <auto|host|docker|os>\n")
		case "auto", "host", "docker", "os":
			runner.sandbox = argument
			runner.printf("Shell backend for this session: %s\n", argument)
		default:
			runner.printError(fmt.Errorf("unknown sandbox backend %q; use auto, host, docker or os", argument))
		}
	case "/compact":
		if len(active) > 0 {
			runner.printError(errors.New("/compact can run only after the current run finishes"))
			break
		}
		requestCtx, cancel := withTimeout(ctx)
		result, err := runner.API.Compact(requestCtx, session.SessionID)
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		runner.printf("Context compaction: %s\n", result.Status)
	case "/cancel":
		requestCtx, cancel := withTimeout(ctx)
		result, err := runner.API.CancelRun(requestCtx, session.SessionID)
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		runner.printf("Cancellation status: %s (queued %d)\n", result.Status, result.Queued)
	case "/new":
		if len(active) > 0 {
			runner.printError(errors.New("/new can run only after the current run finishes"))
			break
		}
		created, err := runner.createSession(ctx)
		if err != nil {
			runner.printError(err)
			break
		}
		previous := session
		runner.previousSession = &previous
		session = created
		runner.printf("New session: %s\n", session.SessionID)
	case "/resume":
		if len(active) > 0 {
			runner.printError(errors.New("/resume can run only after the current run finishes"))
			break
		}
		resumed, err := runner.resumeSession(ctx, argument)
		if err != nil {
			runner.printError(err)
			break
		}
		previous := session
		runner.previousSession = &previous
		session = resumed
		runner.printf("Resumed session: %s\nWorkspace: %s\n", session.SessionID, session.Workspace)
	case "/steer":
		if argument == "" {
			runner.printError(errors.New("usage: /steer <guidance>"))
			break
		}
		requestCtx, cancel := withTimeout(ctx)
		result, err := runner.API.Steer(requestCtx, session.SessionID, argument)
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		runner.printf("Steering queued: run=%s pending=%d\n", result.RunID, result.Pending)
	case "/follow-up", "/followup":
		if argument == "" {
			runner.printError(errors.New("usage: /follow-up <message>"))
			break
		}
		if err := runner.submit(ctx, session, model, argument, active, events, streams); err != nil {
			runner.printError(err)
		}
	case "/approve", "/reject":
		runner.printError(errors.New("no approval is pending"))
	default:
		runner.printError(fmt.Errorf("unknown REPL command %s; enter /help to list commands", command))
	}
	return false, session, model
}

func (runner *Runner) submit(
	ctx context.Context,
	session client.SessionResponse,
	model string,
	message string,
	active map[string]struct{},
	events chan<- runEvent,
	streams chan<- streamResult,
) error {
	requestCtx, cancel := withTimeout(ctx)
	run, err := runner.API.SendMessage(requestCtx, session.SessionID, client.SendMessageRequest{
		Message:     message,
		Mode:        "chat",
		Workspace:   session.Workspace,
		Model:       model,
		BashBackend: runner.sandbox,
	})
	cancel()
	if err != nil {
		return err
	}
	active[run.RunID] = struct{}{}
	runner.printf("run %s: %s\n", run.RunID, run.Status)
	go func() {
		err := runner.API.StreamRunEvents(ctx, session.SessionID, run.RunID, func(value map[string]any) error {
			select {
			case events <- runEvent{runID: run.RunID, value: value}:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		})
		select {
		case streams <- streamResult{runID: run.RunID, err: err}:
		case <-ctx.Done():
		}
	}()
	return nil
}

func (runner *Runner) createSession(ctx context.Context) (client.SessionResponse, error) {
	requestCtx, cancel := withTimeout(ctx)
	created, err := runner.API.CreateSession(requestCtx, client.CreateSessionRequest{
		Workspace: runner.Workspace,
	})
	cancel()
	if err != nil {
		return client.SessionResponse{}, err
	}
	return client.SessionResponse{
		SessionID: created.SessionID,
		Workspace: runner.Workspace,
	}, nil
}

func (runner *Runner) resumeSession(ctx context.Context, target string) (client.SessionResponse, error) {
	target = strings.TrimSpace(target)
	if target == "" || target == "--last" {
		if runner.previousSession == nil {
			return client.SessionResponse{}, errors.New("no previous session is available to resume")
		}
		return *runner.previousSession, nil
	}
	requestCtx, cancel := withTimeout(ctx)
	defer cancel()
	return runner.API.GetSession(requestCtx, target)
}

func (runner *Runner) resolveApproval(
	ctx context.Context,
	sessionID string,
	prompt *approvalPrompt,
	answer string,
) error {
	answer = strings.ToLower(strings.TrimSpace(answer))
	switch answer {
	case "y", "yes":
		requestCtx, cancel := withTimeout(ctx)
		defer cancel()
		return runner.API.Approve(requestCtx, sessionID, prompt.approvalID, false)
	case "a", "all":
		requestCtx, cancel := withTimeout(ctx)
		defer cancel()
		if prompt.kind == "edit" {
			return runner.API.Approve(requestCtx, sessionID, prompt.approvalID, true)
		}
		return runner.API.Approve(requestCtx, sessionID, prompt.approvalID, false)
	default:
		requestCtx, cancel := withTimeout(ctx)
		defer cancel()
		return runner.API.Reject(requestCtx, sessionID, prompt.approvalID)
	}
}

func (runner *Runner) answerQuestion(
	ctx context.Context,
	sessionID string,
	prompt *approvalPrompt,
	answer string,
) error {
	requestCtx, cancel := withTimeout(ctx)
	defer cancel()
	return runner.API.Answer(requestCtx, sessionID, prompt.approvalID, answer)
}

func (runner *Runner) cancelFromSignal(ctx context.Context, sessionID string) {
	requestCtx, cancel := withTimeout(ctx)
	defer cancel()
	result, err := runner.API.CancelRun(requestCtx, sessionID)
	if err != nil {
		runner.printError(err)
		return
	}
	runner.printf("Cancellation status: %s (queued %d)\n", result.Status, result.Queued)
}

// printContextUsage reports how much of the window the next turn would use.
//
// The usable figure, not the raw window: the reserve is held back for the reply,
// so showing the window would advertise headroom that does not exist. Printed
// after the status block so the numbers a TUI would put in a bar are visible in
// the REPL too.
func (runner *Runner) printContextUsage(session client.SessionResponse) {
	usage := session.Context
	if usage == nil {
		return
	}
	due := ""
	if usage.CompactionDue {
		due = "  (compaction due before the next call)"
	}
	runner.printf(
		"context: %s/%s -- %d/%d tokens (%.0f%%)%s\n",
		usage.Provider,
		usage.Model,
		usage.UsedTokens,
		usage.UsableTokens,
		usage.UsedRatio*100,
		due,
	)
}

func (runner *Runner) printStatus(session client.SessionResponse, model string) {
	modelLabel := model
	if modelLabel == "" {
		modelLabel = "route:main"
	}
	// Shown because it decides where commands run, which is the sort of thing a
	// user should never have to remember they changed.
	sandboxLabel := runner.sandbox
	if sandboxLabel == "" {
		sandboxLabel = "project/daemon default"
	}
	runner.printf(
		"Session Status\nsession: %s\nworkspace: %s\nmodel: %s\nsandbox: %s\nrunning: %t\nqueued: %d\npending_steers: %d\nrun: %s\nstage: %s\n",
		session.SessionID,
		session.Workspace,
		modelLabel,
		sandboxLabel,
		session.Agent.Running,
		session.Agent.Queued,
		session.Agent.PendingSteers,
		session.Agent.CurrentRunID,
		session.Agent.Stage,
	)
	runner.printContextUsage(session)
}

func (runner *Runner) prompt(sessionID string) {
	runner.printf("aicode [%s]> ", sessionID)
}

func (runner *Runner) printf(format string, args ...any) {
	runner.outputMu.Lock()
	defer runner.outputMu.Unlock()
	fmt.Fprintf(runner.Out, format, args...)
}

func (runner *Runner) printError(err error) {
	runner.outputMu.Lock()
	defer runner.outputMu.Unlock()
	if runner.firstError == nil {
		runner.firstError = err
	}
	if !runner.Interactive {
		return
	}
	fmt.Fprintf(runner.Err, "Error: %v\n", err)
}

func (runner *Runner) result() error {
	runner.outputMu.Lock()
	defer runner.outputMu.Unlock()
	if runner.Interactive {
		return nil
	}
	return runner.firstError
}

func (runner *Runner) renderEvent(event map[string]any) {
	runner.outputMu.Lock()
	defer runner.outputMu.Unlock()
	renderer.RenderEventTo(runner.Out, event)

	// One tracker per run, reset when the next one starts: in a REPL the
	// failures of an earlier turn are not an account of this one.
	switch stringValue(event["type"]) {
	case "run.started":
		runner.tracker = renderer.NewRunTracker()
	case "final":
		if runner.tracker != nil {
			runner.tracker.PrintSummaryTo(runner.Out)
			runner.tracker = nil
		}
		return
	}
	if runner.tracker != nil {
		runner.tracker.Observe(event)
	}
}

func scanInput(ctx context.Context, reader io.Reader, output chan<- inputLine) {
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		select {
		case output <- inputLine{text: scanner.Text()}:
		case <-ctx.Done():
			return
		}
	}
	err := scanner.Err()
	if err == nil {
		err = io.EOF
	}
	select {
	case output <- inputLine{err: err}:
	case <-ctx.Done():
	}
}

func splitCommand(line string) (string, string) {
	command, argument, found := strings.Cut(strings.TrimSpace(line), " ")
	if !found {
		return strings.ToLower(command), ""
	}
	return strings.ToLower(command), strings.TrimSpace(argument)
}

func isApprovalAnswer(line string, kind string) bool {
	switch strings.ToLower(strings.TrimSpace(line)) {
	case "y", "yes", "n", "no":
		return true
	case "a", "all":
		return kind == "edit"
	default:
		return false
	}
}

func approvalAllHint(kind string) string {
	if kind == "edit" {
		return ", a"
	}
	return ""
}

func withTimeout(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, requestTimeout)
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprint(value)
}

func isTerminal(file *os.File) bool {
	info, err := file.Stat()
	return err == nil && info.Mode()&os.ModeCharDevice != 0
}

const helpText = `REPL commands:
  /status                 Show the session, run, queue, model, sandbox, and context usage
  /model [name]           Show model routes or set the model for later messages
  /sandbox [backend]      Show or set the shell backend for later messages (auto|host|docker|os)
  /compact                Persistently compact the current session context while idle
  /steer <guidance>       Adjust the active run at its next safe boundary
  /follow-up <message>    Queue another run in the current session
  /cancel                 Cancel the active run and preserve later queued runs
  /new                    Create a new session for the current workspace
  /resume [--last|id]     Switch to an existing session
  /exit                   Exit after runs submitted by this REPL finish

Normal input is sent to the current session; while a run is active, it is queued as a follow-up.
The first Ctrl-C cancels the active run; the second exits.
`
