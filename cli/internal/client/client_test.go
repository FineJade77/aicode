package client

import (
	"context"
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
	api := New("http://runtime.test")
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
	api := New("http://runtime.test")
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
