package client

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestStreamEventsReconnectsWithLastEventID(t *testing.T) {
	oldDelay := streamReconnectDelay
	streamReconnectDelay = time.Millisecond
	defer func() { streamReconnectDelay = oldDelay }()

	requestCount := 0
	var secondAfter string
	var secondLastEventID string
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requestCount++
		switch requestCount {
		case 1:
			return sseResponse("id: 1\nevent: run.started\ndata: {\"type\":\"run.started\",\"event_id\":1}\n\n"), nil
		case 2:
			secondAfter = r.URL.Query().Get("after")
			secondLastEventID = r.Header.Get("Last-Event-ID")
			return sseResponse("id: 2\nevent: final\ndata: {\"type\":\"final\",\"event_id\":2,\"summary\":\"done\"}\n\n"), nil
		default:
			return nil, fmt.Errorf("unexpected reconnect request %d", requestCount)
		}
	})}
	var eventTypes []string
	err := api.StreamEvents(context.Background(), "sess_1", func(event map[string]any) error {
		eventTypes = append(eventTypes, event["type"].(string))
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}

	if requestCount != 2 {
		t.Fatalf("requestCount = %d, want 2", requestCount)
	}
	if secondAfter != "1" {
		t.Fatalf("after = %q, want 1", secondAfter)
	}
	if secondLastEventID != "1" {
		t.Fatalf("Last-Event-ID = %q, want 1", secondLastEventID)
	}
	if want := []string{"run.started", "final"}; !reflect.DeepEqual(eventTypes, want) {
		t.Fatalf("events = %#v, want %#v", eventTypes, want)
	}
}

func TestStreamEventsUsesPayloadEventIDWhenIDLineMissing(t *testing.T) {
	oldDelay := streamReconnectDelay
	streamReconnectDelay = time.Millisecond
	defer func() { streamReconnectDelay = oldDelay }()

	requestCount := 0
	var secondAfter string
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requestCount++
		switch requestCount {
		case 1:
			return sseResponse("event: tool.output\ndata: {\"type\":\"tool.output\",\"event_id\":7}\n\n"), nil
		case 2:
			secondAfter = r.URL.Query().Get("after")
			return sseResponse("event: final\ndata: {\"type\":\"final\",\"event_id\":8,\"summary\":\"done\"}\n\n"), nil
		default:
			return nil, fmt.Errorf("unexpected reconnect request %d", requestCount)
		}
	})}
	err := api.StreamEvents(context.Background(), "sess_1", func(event map[string]any) error {
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}

	if secondAfter != "7" {
		t.Fatalf("after = %q, want 7", secondAfter)
	}
}

func TestStreamRunEventsSendsRunID(t *testing.T) {
	var gotRunID string
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		gotRunID = r.URL.Query().Get("run_id")
		return sseResponse("id: 1\nevent: final\ndata: {\"type\":\"final\",\"event_id\":1,\"summary\":\"done\"}\n\n"), nil
	})}

	err := api.StreamRunEvents(context.Background(), "sess_1", "run_abc", func(event map[string]any) error {
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}

	if gotRunID != "run_abc" {
		t.Fatalf("run_id = %q, want run_abc", gotRunID)
	}
}

func TestSendMessageReturnsRunID(t *testing.T) {
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost {
			return nil, fmt.Errorf("method = %s, want POST", r.Method)
		}
		return jsonResponse(`{"status":"queued","run_id":"run_123"}`), nil
	})}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	response, err := api.SendMessage(ctx, "sess_1", SendMessageRequest{
		Message:   "hello",
		Mode:      "default",
		Workspace: "/repo",
		Language:  "zh-CN",
	})
	if err != nil {
		t.Fatal(err)
	}

	if response.Status != "queued" || response.RunID != "run_123" {
		t.Fatalf("response = %#v", response)
	}
}

func TestCancelRunReturnsCancelledRun(t *testing.T) {
	var gotPath string
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		gotPath = r.URL.EscapedPath()
		if r.Method != http.MethodPost {
			return nil, fmt.Errorf("method = %s, want POST", r.Method)
		}
		return jsonResponse(`{"status":"cancelled","run_id":"run_123","queued":2}`), nil
	})}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	response, err := api.CancelRun(ctx, "sess_1")
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/v1/sessions/sess_1/cancel" {
		t.Fatalf("path = %q", gotPath)
	}
	if response.Status != "cancelled" || response.RunID == nil || *response.RunID != "run_123" || response.Queued != 2 {
		t.Fatalf("response = %#v", response)
	}
}

func TestExecutePostsVersionedExecutionContract(t *testing.T) {
	var gotPath string
	var got ExecutionRequest
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		gotPath = r.URL.EscapedPath()
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			return nil, err
		}
		return jsonResponse(`{
			"execution_id":"exec_123",
			"backend":"docker",
			"action":"test",
			"status":"succeeded",
			"exit_code":0,
			"stdout":"ok\n",
			"stderr":"",
			"duration_ms":12,
			"timed_out":false,
			"cancelled":false,
			"future_field":"ignored"
		}`), nil
	})}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	response, err := api.Execute(ctx, ExecutionRequest{
		ExecutionID:    "exec_123",
		Backend:        "docker",
		Action:         "test",
		Workspace:      "/repo",
		TimeoutSeconds: 60,
	})
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/v1/executions" || got.Workspace != "/repo" {
		t.Fatalf("path = %q, request = %#v", gotPath, got)
	}
	if response.Status != "succeeded" || response.ExitCode != 0 || response.ExecutionID != "exec_123" {
		t.Fatalf("response = %#v", response)
	}
}

func TestCancelExecutionEscapesID(t *testing.T) {
	var gotPath string
	api := New("http://runtime.test", "")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		gotPath = r.URL.EscapedPath()
		return jsonResponse(`{"status":"cancelled","execution_id":"exec/1"}`), nil
	})}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	response, err := api.CancelExecution(ctx, "exec/1")
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/v1/executions/exec%2F1/cancel" {
		t.Fatalf("path = %q", gotPath)
	}
	if response.Status != "cancelled" {
		t.Fatalf("response = %#v", response)
	}
}

func TestApproveSendsAcceptAll(t *testing.T) {
	var got map[string]any
	c := New("http://runtime.test", "")
	c.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		return jsonResponse(`{"status":"accepted"}`), nil
	})}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	if err := c.Approve(ctx, "sess_1", "appr_1", true); err != nil {
		t.Fatalf("Approve failed: %v", err)
	}
	if got["accept_all"] != true {
		t.Fatalf("expected accept_all=true, got %v", got)
	}
}

func TestApprovalAlreadyResolvedIsIdempotent(t *testing.T) {
	for _, tt := range []struct {
		name string
		call func(Client, context.Context) error
	}{
		{
			name: "approve",
			call: func(c Client, ctx context.Context) error {
				return c.Approve(ctx, "sess_1", "appr_1", false)
			},
		},
		{
			name: "reject",
			call: func(c Client, ctx context.Context) error {
				return c.Reject(ctx, "sess_1", "appr_1")
			},
		},
	} {
		t.Run(tt.name, func(t *testing.T) {
			c := New("http://runtime.test", "")
			c.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				return &http.Response{
					StatusCode: http.StatusNotFound,
					Status:     "404 Not Found",
					Body:       io.NopCloser(strings.NewReader(`{"detail":"approval not found or already resolved"}`)),
				}, nil
			})}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()

			if err := tt.call(c, ctx); err != nil {
				t.Fatalf("expected already-resolved approval to be idempotent, got %v", err)
			}
		})
	}
}

func TestRejectReturnsOtherNotFoundErrors(t *testing.T) {
	c := New("http://runtime.test", "")
	c.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusNotFound,
			Status:     "404 Not Found",
			Body:       io.NopCloser(strings.NewReader(`{"detail":"session not found"}`)),
		}, nil
	})}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	err := c.Reject(ctx, "sess_1", "appr_1")
	if err == nil {
		t.Fatal("expected non-approval 404 to stay fatal")
	}
	if !strings.Contains(err.Error(), "session not found") {
		t.Fatalf("expected original error detail, got %v", err)
	}
}

func TestRequestsIncludeAuthorizationHeaderWhenTokenSet(t *testing.T) {
	var gotHeader string
	c := New("http://runtime.test", "secret-token")
	c.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		gotHeader = r.Header.Get("Authorization")
		return jsonResponse(`{}`), nil
	})}

	if _, err := c.GetJSON(context.Background(), "/v1/sessions"); err != nil {
		t.Fatalf("GetJSON failed: %v", err)
	}
	if gotHeader != "Bearer secret-token" {
		t.Fatalf("expected Authorization header %q, got %q", "Bearer secret-token", gotHeader)
	}
}

func TestRequestsOmitAuthorizationHeaderWhenNoToken(t *testing.T) {
	var gotHeader string
	sawRequest := false
	c := New("http://runtime.test", "")
	c.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		sawRequest = true
		gotHeader = r.Header.Get("Authorization")
		return jsonResponse(`{}`), nil
	})}

	if _, err := c.GetJSON(context.Background(), "/v1/sessions"); err != nil {
		t.Fatalf("GetJSON failed: %v", err)
	}
	if !sawRequest {
		t.Fatalf("expected server to receive a request")
	}
	if gotHeader != "" {
		t.Fatalf("expected no Authorization header, got %q", gotHeader)
	}
}

func TestGetJSONUnauthorizedExplainsTokenRecovery(t *testing.T) {
	c := New("http://runtime.test", "stale-token")
	c.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return unauthorizedResponse(), nil
	})}
	_, err := c.GetJSON(context.Background(), "/v1/sessions")
	if err == nil {
		t.Fatal("expected error")
	}
	assertRuntimeAuthHint(t, err)
}

func TestPostJSONUnauthorizedExplainsTokenRecovery(t *testing.T) {
	c := New("http://runtime.test", "stale-token")
	c.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return unauthorizedResponse(), nil
	})}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	err := c.Reject(ctx, "sess_1", "appr_1")
	if err == nil {
		t.Fatal("expected error")
	}
	assertRuntimeAuthHint(t, err)
}

func TestStreamUnauthorizedExplainsTokenRecovery(t *testing.T) {
	api := New("http://runtime.test", "stale-token")
	api.http = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusUnauthorized,
			Status:     "401 Unauthorized",
			Body:       io.NopCloser(strings.NewReader(`{"detail":"unauthorized"}`)),
		}, nil
	})}

	err := api.StreamEvents(context.Background(), "sess_1", func(event map[string]any) error {
		return nil
	})
	if err == nil {
		t.Fatal("expected error")
	}
	assertRuntimeAuthHint(t, err)
}

func assertRuntimeAuthHint(t *testing.T, err error) {
	t.Helper()
	text := err.Error()
	for _, want := range []string{"Runtime 认证失败", "runtime.token", "aicode daemon stop", "aicode daemon start"} {
		if !strings.Contains(text, want) {
			t.Fatalf("error %q does not contain %q", text, want)
		}
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) {
	return fn(req)
}

func sseResponse(body string) *http.Response {
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func jsonResponse(body string) *http.Response {
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func unauthorizedResponse() *http.Response {
	return &http.Response{
		StatusCode: http.StatusUnauthorized,
		Status:     "401 Unauthorized",
		Body:       io.NopCloser(strings.NewReader(`{"detail":"unauthorized"}`)),
	}
}
