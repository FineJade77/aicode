package chatcmd

import (
	"bytes"
	"context"
	"io"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/FineJade77/aicode/cli/internal/client"
)

type fakeAPI struct {
	mu sync.Mutex

	sessionCounter int
	runCounter     int
	sessions       map[string]client.SessionResponse
	lastSessionID  string
	sends          []client.SendMessageRequest
	sendSessions   []string
	steers         []string
	cancelCalls    int
	compactCalls   int
	modelCalls     int
	approveCalls   int
	rejectCalls    int

	blockRuns         bool
	approvalRuns      bool
	runRelease        chan struct{}
	approvalRelease   chan struct{}
	approvalRequested chan struct{}
	releaseOnce       sync.Once
	approvalOnce      sync.Once
	sent              chan struct{}
}

func newFakeAPI() *fakeAPI {
	return &fakeAPI{
		sessions:          map[string]client.SessionResponse{},
		runRelease:        make(chan struct{}),
		approvalRelease:   make(chan struct{}),
		approvalRequested: make(chan struct{}, 1),
		sent:              make(chan struct{}, 8),
	}
}

func (api *fakeAPI) CreateSession(_ context.Context, request client.CreateSessionRequest) (client.CreateSessionResponse, error) {
	api.mu.Lock()
	defer api.mu.Unlock()
	api.sessionCounter++
	id := "sess_" + string(rune('0'+api.sessionCounter))
	api.sessions[id] = client.SessionResponse{SessionID: id, Workspace: request.Workspace}
	api.lastSessionID = id
	return client.CreateSessionResponse{SessionID: id}, nil
}

func (api *fakeAPI) GetSession(_ context.Context, id string) (client.SessionResponse, error) {
	api.mu.Lock()
	defer api.mu.Unlock()
	session := api.sessions[id]
	session.Agent = client.SessionAgentStatus{Running: api.blockRuns, Queued: len(api.sends), CurrentRunID: "run_1", Stage: "model.stream"}
	return session, nil
}

func (api *fakeAPI) LastSession(_ context.Context) (client.SessionResponse, bool, error) {
	api.mu.Lock()
	defer api.mu.Unlock()
	session, ok := api.sessions[api.lastSessionID]
	return session, ok, nil
}

func (api *fakeAPI) SendMessage(_ context.Context, sessionID string, request client.SendMessageRequest) (client.SendMessageResponse, error) {
	api.mu.Lock()
	api.runCounter++
	runID := "run_" + string(rune('0'+api.runCounter))
	api.sends = append(api.sends, request)
	api.sendSessions = append(api.sendSessions, sessionID)
	api.mu.Unlock()
	select {
	case api.sent <- struct{}{}:
	default:
	}
	return client.SendMessageResponse{Status: "accepted", RunID: runID}, nil
}

func (api *fakeAPI) StreamRunEvents(ctx context.Context, _ string, runID string, handle func(map[string]any) error) error {
	if err := handle(map[string]any{"type": "run.started", "run_id": runID, "message": "started"}); err != nil {
		return err
	}
	api.mu.Lock()
	block := api.blockRuns
	release := api.runRelease
	approval := api.approvalRuns && runID == "run_1"
	approvalRelease := api.approvalRelease
	api.mu.Unlock()
	if approval {
		if err := handle(map[string]any{
			"type":        "approval.requested",
			"run_id":      runID,
			"approval_id": "appr_1",
			"kind":        "tool",
			"message":     "allow?",
		}); err != nil {
			return err
		}
		select {
		case api.approvalRequested <- struct{}{}:
		default:
		}
		select {
		case <-approvalRelease:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if block {
		select {
		case <-release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return handle(map[string]any{"type": "final", "run_id": runID, "summary": "done"})
}

func (api *fakeAPI) CancelRun(_ context.Context, _ string) (client.CancelRunResponse, error) {
	api.mu.Lock()
	api.cancelCalls++
	api.mu.Unlock()
	api.release()
	runID := "run_1"
	return client.CancelRunResponse{Status: "cancelled", RunID: &runID}, nil
}

func (api *fakeAPI) Steer(_ context.Context, _ string, message string) (client.SteerResponse, error) {
	api.mu.Lock()
	api.steers = append(api.steers, message)
	api.mu.Unlock()
	api.release()
	return client.SteerResponse{Status: "queued", RunID: "run_1", Pending: 1}, nil
}

func (api *fakeAPI) Compact(_ context.Context, _ string) (client.CompactResponse, error) {
	api.mu.Lock()
	api.compactCalls++
	api.mu.Unlock()
	return client.CompactResponse{Status: "compacted", Compaction: map[string]any{"compaction_id": 1}}, nil
}

func (api *fakeAPI) Approve(context.Context, string, string, bool) error {
	api.mu.Lock()
	api.approveCalls++
	api.mu.Unlock()
	api.releaseApproval()
	return nil
}

func (api *fakeAPI) Reject(context.Context, string, string) error {
	api.mu.Lock()
	api.rejectCalls++
	api.mu.Unlock()
	api.releaseApproval()
	return nil
}

func (api *fakeAPI) GetJSON(_ context.Context, _ string) (any, error) {
	api.mu.Lock()
	api.modelCalls++
	api.mu.Unlock()
	return map[string]any{
		"provider": map[string]any{"primary": "fake", "primary_configured": true},
		"routes":   map[string]any{"main": "fake-main"},
	}, nil
}

func (api *fakeAPI) release() {
	api.releaseOnce.Do(func() { close(api.runRelease) })
}

func (api *fakeAPI) releaseApproval() {
	api.approvalOnce.Do(func() { close(api.approvalRelease) })
}

func TestNonTTYQueuesFollowUpsWithModelOverride(t *testing.T) {
	api := newFakeAPI()
	var out bytes.Buffer
	var stderr bytes.Buffer
	runner := Runner{
		API:       api,
		In:        strings.NewReader("/model local-coder\nfirst task\nsecond task\n"),
		Out:       &out,
		Err:       &stderr,
		Workspace: "/workspace",
	}

	if err := runner.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr = %q", stderr.String())
	}
	if strings.Contains(out.String(), "aicode [") {
		t.Fatalf("non-TTY output contains a prompt: %q", out.String())
	}
	if len(api.sends) != 2 {
		t.Fatalf("sends = %#v", api.sends)
	}
	for index, request := range api.sends {
		if request.Model != "local-coder" {
			t.Fatalf("send %d model = %q", index, request.Model)
		}
		if api.sendSessions[index] != "sess_1" {
			t.Fatalf("send %d session = %q", index, api.sendSessions[index])
		}
	}
}

func TestREPLControlCommandsManagePersistentSession(t *testing.T) {
	api := newFakeAPI()
	var out bytes.Buffer
	var stderr bytes.Buffer
	runner := Runner{
		API: api,
		In: strings.NewReader(
			"/status\n/model local-coder\n/model\n/compact\n/new\n/resume --last\n/exit\n",
		),
		Out:       &out,
		Err:       &stderr,
		Workspace: "/workspace",
	}

	if err := runner.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr = %q", stderr.String())
	}
	if api.sessionCounter != 2 || api.compactCalls != 1 || api.modelCalls != 1 {
		t.Fatalf("create=%d compact=%d models=%d", api.sessionCounter, api.compactCalls, api.modelCalls)
	}
	for _, expected := range []string{"Session Status", "Current message model override: local-coder", "Context compaction: compacted", "New session: sess_2", "Resumed session: sess_1"} {
		if !strings.Contains(out.String(), expected) {
			t.Fatalf("output missing %q: %s", expected, out.String())
		}
	}
}

func TestResumeLastUsesSessionBeforeREPLBootstrap(t *testing.T) {
	api := newFakeAPI()
	api.sessionCounter = 1
	api.sessions["sess_1"] = client.SessionResponse{
		SessionID: "sess_1",
		Workspace: "/previous",
	}
	api.lastSessionID = "sess_1"
	var out bytes.Buffer
	runner := Runner{
		API:       api,
		In:        strings.NewReader("/resume --last\n/exit\n"),
		Out:       &out,
		Err:       io.Discard,
		Workspace: "/workspace",
	}

	if err := runner.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Resumed session: sess_1\nWorkspace: /previous") {
		t.Fatalf("previous session was not resumed: %q", out.String())
	}
}

func TestSteerAppliesWhileRunIsActive(t *testing.T) {
	api := newFakeAPI()
	api.blockRuns = true
	var out bytes.Buffer
	var stderr bytes.Buffer
	runner := Runner{
		API:         api,
		In:          strings.NewReader("first task\n/steer only update tests\n"),
		Out:         &out,
		Err:         &stderr,
		Workspace:   "/workspace",
		Interactive: true,
	}

	if err := runner.Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr = %q", stderr.String())
	}
	if len(api.steers) != 1 || api.steers[0] != "only update tests" {
		t.Fatalf("steers = %#v", api.steers)
	}
	if !strings.Contains(out.String(), "aicode [sess_1]>") {
		t.Fatalf("interactive prompt missing: %q", out.String())
	}
}

func TestNonTTYReturnsCommandErrorsWithoutPromptNoise(t *testing.T) {
	api := newFakeAPI()
	var out bytes.Buffer
	var stderr bytes.Buffer
	runner := Runner{
		API:       api,
		In:        strings.NewReader("/unknown\n"),
		Out:       &out,
		Err:       &stderr,
		Workspace: "/workspace",
	}

	err := runner.Run(context.Background())

	if err == nil || !strings.Contains(err.Error(), "unknown REPL command") {
		t.Fatalf("error = %v", err)
	}
	if stderr.Len() != 0 {
		t.Fatalf("runner should return non-TTY errors to its caller: %q", stderr.String())
	}
	if strings.Contains(out.String(), "aicode [") {
		t.Fatalf("non-TTY output contains a prompt: %q", out.String())
	}
}

func TestApprovalDoesNotConsumePendingFollowUpAsDecision(t *testing.T) {
	api := newFakeAPI()
	api.approvalRuns = true
	inputReader, inputWriter := io.Pipe()
	defer inputReader.Close()
	var out bytes.Buffer
	var stderr bytes.Buffer
	runner := Runner{
		API:         api,
		In:          inputReader,
		Out:         &out,
		Err:         &stderr,
		Workspace:   "/workspace",
		Interactive: true,
	}
	done := make(chan error, 1)
	go func() { done <- runner.Run(context.Background()) }()
	if _, err := inputWriter.Write([]byte("first task\n")); err != nil {
		t.Fatal(err)
	}
	select {
	case <-api.approvalRequested:
	case <-time.After(time.Second):
		t.Fatal("approval was not requested")
	}
	deadline := time.Now().Add(time.Second)
	for {
		runner.outputMu.Lock()
		prompted := strings.Contains(out.String(), "y=allow")
		runner.outputMu.Unlock()
		if prompted {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("approval prompt was not rendered")
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := inputWriter.Write([]byte("follow-up task\ny\n")); err != nil {
		t.Fatal(err)
	}
	if err := inputWriter.Close(); err != nil {
		t.Fatal(err)
	}

	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("REPL did not finish after approval")
	}

	api.mu.Lock()
	defer api.mu.Unlock()
	if len(api.sends) != 2 || api.sends[1].Message != "follow-up task" {
		t.Fatalf("sends = %#v", api.sends)
	}
	if api.approveCalls != 1 || api.rejectCalls != 0 {
		t.Fatalf("approve=%d reject=%d", api.approveCalls, api.rejectCalls)
	}
}

func TestCtrlCFirstCancelsActiveRun(t *testing.T) {
	api := newFakeAPI()
	api.blockRuns = true
	inputReader, inputWriter := io.Pipe()
	defer inputReader.Close()
	signals := make(chan os.Signal, 2)
	var out bytes.Buffer
	var stderr bytes.Buffer
	runner := Runner{
		API:         api,
		In:          inputReader,
		Out:         &out,
		Err:         &stderr,
		Workspace:   "/workspace",
		Interactive: true,
		Signals:     signals,
	}
	done := make(chan error, 1)
	go func() { done <- runner.Run(context.Background()) }()
	if _, err := inputWriter.Write([]byte("first task\n")); err != nil {
		t.Fatal(err)
	}
	select {
	case <-api.sent:
	case <-time.After(time.Second):
		t.Fatal("run was not submitted")
	}

	signals <- os.Interrupt
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		signals <- os.Interrupt
		select {
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(time.Second):
			t.Fatal("REPL did not exit after interrupt")
		}
	}
	inputWriter.Close()

	api.mu.Lock()
	cancelCalls := api.cancelCalls
	api.mu.Unlock()
	if cancelCalls != 1 {
		t.Fatalf("cancel calls = %d", cancelCalls)
	}
}
