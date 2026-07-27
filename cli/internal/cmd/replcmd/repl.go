// Package replcmd implements the persistent `aicode chat` interactive session.
package replcmd

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
	Language    string
	Interactive bool
	Signals     <-chan os.Signal

	outputMu        sync.Mutex
	previousSession *client.SessionResponse
	firstError      error
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
		Language:    cfg.UI.Language,
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
	if strings.TrimSpace(runner.Language) == "" {
		runner.Language = "zh-CN"
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

	runner.printf("会话: %s\n", session.SessionID)
	if runner.Interactive {
		runner.printf("常驻 REPL 已启动；输入 /help 查看命令。\n")
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
			runner.printf("\n正在取消当前 run；再次按 Ctrl-C 退出。\n")
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
					runner.printf("仍有待确认操作；请输入 y、n%s，或使用 /cancel。\n", approvalAllHint(pending.kind))
				}
				if runner.Interactive {
					runner.prompt(session.SessionID)
				}
				continue
			}
			command, argument := splitCommand(line)
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
			if stringValue(event.value["type"]) == "approval.requested" {
				approvalID := stringValue(event.value["approval_id"])
				if approvalID == "" {
					runner.printError(errors.New("approval.requested 缺少 approval_id"))
					continue
				}
				kind := stringValue(event.value["kind"])
				if kind == "edit" {
					if diff := stringValue(event.value["diff"]); diff != "" {
						runner.printf("%s\n", diff)
					}
					runner.printf("应用这个编辑吗？[y=应用 / a=应用并允许本会话后续编辑 / n=拒绝]: ")
				} else {
					runner.printf("允许执行这个工具操作吗？[y=允许 / n=拒绝]: ")
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
			runner.printf("当前消息模型覆盖: %s\n", model)
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
			runner.printf("当前消息模型覆盖: %s\n\n", model)
		}
		runner.printf("%s", renderer.ModelRoutesTable(value))
	case "/compact":
		if len(active) > 0 {
			runner.printError(errors.New("/compact 只能在当前 run 完成后执行"))
			break
		}
		requestCtx, cancel := withTimeout(ctx)
		result, err := runner.API.Compact(requestCtx, session.SessionID)
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		runner.printf("上下文压缩: %s\n", result.Status)
	case "/cancel":
		requestCtx, cancel := withTimeout(ctx)
		result, err := runner.API.CancelRun(requestCtx, session.SessionID)
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		runner.printf("取消状态: %s（排队 %d）\n", result.Status, result.Queued)
	case "/new":
		if len(active) > 0 {
			runner.printError(errors.New("/new 只能在当前 run 完成后执行"))
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
		runner.printf("新会话: %s\n", session.SessionID)
	case "/resume":
		if len(active) > 0 {
			runner.printError(errors.New("/resume 只能在当前 run 完成后执行"))
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
		runner.printf("恢复会话: %s\n工作区: %s\n", session.SessionID, session.Workspace)
	case "/steer":
		if argument == "" {
			runner.printError(errors.New("用法: /steer <guidance>"))
			break
		}
		requestCtx, cancel := withTimeout(ctx)
		result, err := runner.API.Steer(requestCtx, session.SessionID, argument)
		cancel()
		if err != nil {
			runner.printError(err)
			break
		}
		runner.printf("steer 已排队: run=%s pending=%d\n", result.RunID, result.Pending)
	case "/follow-up", "/followup":
		if argument == "" {
			runner.printError(errors.New("用法: /follow-up <message>"))
			break
		}
		if err := runner.submit(ctx, session, model, argument, active, events, streams); err != nil {
			runner.printError(err)
		}
	case "/approve", "/reject":
		runner.printError(errors.New("当前没有待确认操作"))
	default:
		runner.printError(fmt.Errorf("未知 REPL 命令 %s；输入 /help 查看命令", command))
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
		Message:   message,
		Mode:      "chat",
		Workspace: session.Workspace,
		Language:  session.Language,
		Model:     model,
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
		Language:  runner.Language,
	})
	cancel()
	if err != nil {
		return client.SessionResponse{}, err
	}
	return client.SessionResponse{
		SessionID: created.SessionID,
		Workspace: runner.Workspace,
		Language:  runner.Language,
	}, nil
}

func (runner *Runner) resumeSession(ctx context.Context, target string) (client.SessionResponse, error) {
	target = strings.TrimSpace(target)
	if target == "" || target == "--last" {
		if runner.previousSession == nil {
			return client.SessionResponse{}, errors.New("没有可恢复的上一条 session")
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

func (runner *Runner) cancelFromSignal(ctx context.Context, sessionID string) {
	requestCtx, cancel := withTimeout(ctx)
	defer cancel()
	result, err := runner.API.CancelRun(requestCtx, sessionID)
	if err != nil {
		runner.printError(err)
		return
	}
	runner.printf("取消状态: %s（排队 %d）\n", result.Status, result.Queued)
}

func (runner *Runner) printStatus(session client.SessionResponse, model string) {
	modelLabel := model
	if modelLabel == "" {
		modelLabel = "route:main"
	}
	runner.printf(
		"Session Status\nsession: %s\nworkspace: %s\nlanguage: %s\nmodel: %s\nrunning: %t\nqueued: %d\npending_steers: %d\nrun: %s\nstage: %s\n",
		session.SessionID,
		session.Workspace,
		session.Language,
		modelLabel,
		session.Agent.Running,
		session.Agent.Queued,
		session.Agent.PendingSteers,
		session.Agent.CurrentRunID,
		session.Agent.Stage,
	)
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
	fmt.Fprintf(runner.Err, "错误: %v\n", err)
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
		return "、a"
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

const helpText = `REPL 命令:
  /status                 查看当前 session、run、queue 和模型覆盖
  /model [name]           查看模型路由，或设置后续消息的模型
  /compact                空闲时持久化压缩当前 session 上下文
  /steer <guidance>       在当前 run 的下一个安全边界调整执行
  /follow-up <message>    在当前 session 排队后续任务
  /cancel                 取消当前 run，保留后续队列
  /new                    为当前 workspace 创建新 session
  /resume [--last|id]     切换到已有 session
  /exit                   等待本 REPL 已提交的 run 完成后退出

普通输入会发送到当前 session；run 活跃时会作为 follow-up 排队。
Ctrl-C 第一次取消当前 run，第二次退出。
`
