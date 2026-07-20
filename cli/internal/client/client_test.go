package client

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
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
			return sseResponse("id: 1\nevent: plan.created\ndata: {\"type\":\"plan.created\",\"event_id\":1}\n\n"), nil
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
	if want := []string{"plan.created", "final"}; !reflect.DeepEqual(eventTypes, want) {
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

func TestApproveSendsAcceptAll(t *testing.T) {
	var got map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"accepted"}`))
	}))
	defer server.Close()

	c := New(server.URL, "")
	if err := c.Approve(context.Background(), "sess_1", "appr_1", true); err != nil {
		t.Fatalf("Approve failed: %v", err)
	}
	if got["accept_all"] != true {
		t.Fatalf("expected accept_all=true, got %v", got)
	}
}

func TestRequestsIncludeAuthorizationHeaderWhenTokenSet(t *testing.T) {
	var gotHeader string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHeader = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()

	c := New(server.URL, "secret-token")
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
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawRequest = true
		gotHeader = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()

	c := New(server.URL, "")
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
