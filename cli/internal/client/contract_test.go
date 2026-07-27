package client

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"
)

type httpContractFixture struct {
	ContractVersion string                     `json:"contract_version"`
	Responses       map[string]json.RawMessage `json:"responses"`
}

type sseContractFixture struct {
	ContractVersion    string           `json:"contract_version"`
	Events             []map[string]any `json:"events"`
	ForwardCompatEvent map[string]any   `json:"forward_compat_event"`
}

type applicationContractFixture struct {
	ContractVersion   string              `json:"contract_version"`
	TurnRequest       SendMessageRequest  `json:"turn_request"`
	RunReceipt        SendMessageResponse `json:"run_receipt"`
	RunControl        CancelRunResponse   `json:"run_control"`
	SteerReceipt      SteerResponse       `json:"steer_receipt"`
	CompactionReceipt CompactResponse     `json:"compaction_receipt"`
	SessionSnapshot   SessionResponse     `json:"session_snapshot"`
}

func TestApplicationContractFixtureMatchesClientTypes(t *testing.T) {
	var fixture applicationContractFixture
	loadContractFixture(t, "application-contract.v1.json", &fixture)

	if fixture.ContractVersion != "1.0" {
		t.Fatalf("application contract version = %q", fixture.ContractVersion)
	}
	if fixture.TurnRequest.Model != "fixture-model" || fixture.TurnRequest.Workspace != "/workspace" {
		t.Fatalf("turn request = %#v", fixture.TurnRequest)
	}
	if fixture.RunReceipt.RunID != "run_fixture" || fixture.RunReceipt.Status != "accepted" {
		t.Fatalf("run receipt = %#v", fixture.RunReceipt)
	}
	if fixture.RunControl.RunID == nil || *fixture.RunControl.RunID != "run_fixture" {
		t.Fatalf("run control = %#v", fixture.RunControl)
	}
	if fixture.SteerReceipt.Pending != 1 || fixture.CompactionReceipt.Status != "compacted" {
		t.Fatalf("control receipts = %#v %#v", fixture.SteerReceipt, fixture.CompactionReceipt)
	}
	if fixture.SessionSnapshot.Agent.CurrentRunID != "run_fixture" {
		t.Fatalf("session snapshot = %#v", fixture.SessionSnapshot)
	}
}

func TestHTTPResponseFixtureMatchesClientTypes(t *testing.T) {
	var fixture httpContractFixture
	loadContractFixture(t, "http-responses.v2.json", &fixture)
	if fixture.ContractVersion != "2.0" {
		t.Fatalf("contract version = %q", fixture.ContractVersion)
	}

	cancelCalls := 0
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		key := ""
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/meta/contract":
			key = "api_contract"
		case r.Method == http.MethodGet && r.URL.Path == "/v1/trust" && r.URL.Query().Get("workspace") != "":
			key = "trust_status"
		case r.Method == http.MethodGet && r.URL.Path == "/v1/trust":
			key = "trust_list"
		case r.Method == http.MethodPost && r.URL.Path == "/v1/trust/remove":
			key = "trust_removed"
		case r.Method == http.MethodPost && r.URL.Path == "/v1/trust":
			key = "trust_project"
		case r.Method == http.MethodPost && r.URL.Path == "/v1/executions":
			key = "execute_sandbox"
		case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/v1/executions/"):
			key = "cancel_execution"
		case r.Method == http.MethodPost && r.URL.Path == "/v1/sessions":
			key = "create_session"
		case r.Method == http.MethodGet && r.URL.Path == "/v1/sessions/sess_fixture":
			key = "session_status"
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/messages"):
			key = "send_message"
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/steer"):
			key = "steer_session"
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/compact"):
			key = "compact_session"
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/cancel"):
			cancelCalls++
			key = "cancel_run_cancelled"
			if cancelCalls == 2 {
				key = "cancel_run_idle"
			}
		}
		payload, ok := fixture.Responses[key]
		if !ok {
			return nil, fmt.Errorf("missing fixture response for %s %s", r.Method, r.URL.Path)
		}
		return jsonResponse(string(payload)), nil
	})}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	contract, err := api.Contract(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if contract.ContractVersion != "2.0" || contract.MinSupportedVersion != "2.0" {
		t.Fatalf("contract = %#v", contract)
	}
	if contract.Transports["sse"].EventSchema != "v2" {
		t.Fatalf("SSE contract = %#v", contract.Transports["sse"])
	}
	if contract.Application.Version != "1.0" || contract.Application.Types["turn"] != "TurnRequest" {
		t.Fatalf("application contract = %#v", contract.Application)
	}

	created, err := api.CreateSession(ctx, CreateSessionRequest{Workspace: "/workspace", Language: "zh-CN"})
	if err != nil {
		t.Fatal(err)
	}
	if created.SessionID != "sess_fixture" {
		t.Fatalf("session_id = %q", created.SessionID)
	}

	sent, err := api.SendMessage(ctx, "sess_fixture", SendMessageRequest{
		Message: "hello", Mode: "default", Workspace: "/workspace", Language: "zh-CN",
	})
	if err != nil {
		t.Fatal(err)
	}
	if sent.Status != "accepted" || sent.RunID != "run_fixture" {
		t.Fatalf("send response = %#v", sent)
	}

	status, err := api.GetSession(ctx, "sess_fixture")
	if err != nil {
		t.Fatal(err)
	}
	if status.Agent.CurrentRunID != "run_fixture" || status.Agent.PendingSteers != 0 {
		t.Fatalf("session status = %#v", status)
	}

	steered, err := api.Steer(ctx, "sess_fixture", "keep tests focused")
	if err != nil {
		t.Fatal(err)
	}
	if steered.RunID != "run_fixture" || steered.Pending != 1 {
		t.Fatalf("steer response = %#v", steered)
	}

	compacted, err := api.Compact(ctx, "sess_fixture")
	if err != nil {
		t.Fatal(err)
	}
	if compacted.Status != "compacted" || compacted.Compaction["session_id"] != "sess_fixture" {
		t.Fatalf("compact response = %#v", compacted)
	}

	cancelled, err := api.CancelRun(ctx, "sess_fixture")
	if err != nil {
		t.Fatal(err)
	}
	if cancelled.RunID == nil || *cancelled.RunID != "run_fixture" || cancelled.Queued != 1 {
		t.Fatalf("cancelled response = %#v", cancelled)
	}

	idle, err := api.CancelRun(ctx, "sess_fixture")
	if err != nil {
		t.Fatal(err)
	}
	if idle.Status != "idle" || idle.RunID != nil || idle.Queued != 0 {
		t.Fatalf("idle response = %#v", idle)
	}

	execution, err := api.Execute(ctx, ExecutionRequest{
		ExecutionID: "exec_fixture", Backend: "docker", Action: "test", Workspace: "/workspace", TimeoutSeconds: 60,
	})
	if err != nil {
		t.Fatal(err)
	}
	if execution.Status != "succeeded" || execution.ExecutionID != "exec_fixture" {
		t.Fatalf("execution response = %#v", execution)
	}
	cancelledExecution, err := api.CancelExecution(ctx, "exec_fixture")
	if err != nil {
		t.Fatal(err)
	}
	if cancelledExecution.Status != "cancelled" || cancelledExecution.ExecutionID != "exec_fixture" {
		t.Fatalf("cancel execution response = %#v", cancelledExecution)
	}

	trustStatus, err := api.GetTrust(ctx, "/workspace")
	if err != nil {
		t.Fatal(err)
	}
	if trustStatus.Level != "untrusted" || trustStatus.Workspace != "/workspace" {
		t.Fatalf("trust status = %#v", trustStatus)
	}
	trusted, err := api.TrustProject(ctx, "/workspace")
	if err != nil {
		t.Fatal(err)
	}
	if trusted.Level != "trusted" {
		t.Fatalf("trusted = %#v", trusted)
	}
	trustList, err := api.ListTrust(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(trustList.Projects) != 1 || trustList.Projects[0].Level != "trusted" {
		t.Fatalf("trust list = %#v", trustList)
	}
	removed, err := api.RemoveTrust(ctx, "/workspace")
	if err != nil {
		t.Fatal(err)
	}
	if !removed.Removed || removed.Level != "untrusted" {
		t.Fatalf("removed trust = %#v", removed)
	}
}

func TestSSEFixtureStreamsAllV2EventsUntilFinal(t *testing.T) {
	var fixture sseContractFixture
	loadContractFixture(t, "sse-events.v2.json", &fixture)

	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return sseResponse(encodeFixtureEvents(t, fixture.Events)), nil
	})}

	var received []map[string]any
	err := api.StreamEvents(context.Background(), "sess_fixture", func(event map[string]any) error {
		received = append(received, event)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(received) != len(fixture.Events) {
		t.Fatalf("received %d events, want %d", len(received), len(fixture.Events))
	}

	gotTypes := eventTypes(received)
	wantTypes := eventTypes(fixture.Events)
	if !reflect.DeepEqual(gotTypes, wantTypes) {
		t.Fatalf("event types = %#v, want %#v", gotTypes, wantTypes)
	}
	if gotTypes[len(gotTypes)-1] != "final" {
		t.Fatalf("last event = %q, want final", gotTypes[len(gotTypes)-1])
	}
	if _, ok := received[5]["future_field"]; !ok {
		t.Fatal("unknown fields must be preserved in event maps")
	}
}

func TestSSEUnknownEventPassesThroughAndDoesNotTerminate(t *testing.T) {
	var fixture sseContractFixture
	loadContractFixture(t, "sse-events.v2.json", &fixture)
	finalEvent := map[string]any{
		"event_id": float64(101),
		"type":     "final",
		"summary":  "done",
	}
	events := []map[string]any{fixture.ForwardCompatEvent, finalEvent}

	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return sseResponse(encodeFixtureEvents(t, events)), nil
	})}

	var received []map[string]any
	err := api.StreamEvents(context.Background(), "sess_fixture", func(event map[string]any) error {
		received = append(received, event)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"future.progress", "final"}; !reflect.DeepEqual(eventTypes(received), want) {
		t.Fatalf("events = %#v, want %#v", eventTypes(received), want)
	}
	if _, ok := received[0]["future_field"]; !ok {
		t.Fatal("forward-compatible event fields were dropped")
	}
}

func loadContractFixture(t *testing.T, name string, target any) {
	t.Helper()
	_, currentFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot resolve contract fixture path")
	}
	path := filepath.Join(filepath.Dir(currentFile), "..", "..", "..", "schemas", "fixtures", name)
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(content, target); err != nil {
		t.Fatal(err)
	}
}

func encodeFixtureEvents(t *testing.T, events []map[string]any) string {
	t.Helper()
	var out strings.Builder
	for _, event := range events {
		payload, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		id, ok := event["event_id"].(float64)
		if !ok {
			t.Fatalf("event_id is not a JSON number: %#v", event["event_id"])
		}
		eventType, ok := event["type"].(string)
		if !ok {
			t.Fatalf("event type is not a string: %#v", event["type"])
		}
		fmt.Fprintf(&out, "id: %.0f\nevent: %s\ndata: %s\n\n", id, eventType, payload)
	}
	return out.String()
}

func eventTypes(events []map[string]any) []string {
	types := make([]string, 0, len(events))
	for _, event := range events {
		eventType, _ := event["type"].(string)
		types = append(types, eventType)
	}
	return types
}
