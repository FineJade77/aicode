package client

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type Client struct {
	baseURL string
	http    *http.Client
}

var streamReconnectDelay = 250 * time.Millisecond

type CreateSessionRequest struct {
	Workspace string `json:"workspace"`
	Language  string `json:"language"`
}

type CreateSessionResponse struct {
	SessionID string `json:"session_id"`
}

type SendMessageRequest struct {
	Message   string `json:"message"`
	Mode      string `json:"mode"`
	Workspace string `json:"workspace"`
	Language  string `json:"language"`
}

type SendMessageResponse struct {
	Status string `json:"status"`
	RunID  string `json:"run_id"`
}

type ApprovalRequest struct {
	ApprovalID string `json:"approval_id"`
}

type ApproveRequest struct {
	ApprovalID string `json:"approval_id"`
	AcceptAll  bool   `json:"accept_all"`
}

func New(baseURL string) Client {
	return Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		http: &http.Client{
			Timeout: 0,
		},
	}
}

func (c Client) GetJSON(ctx context.Context, path string) (any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return nil, err
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("GET %s failed: %s: %s", path, resp.Status, strings.TrimSpace(string(body)))
	}

	var value any
	if err := json.NewDecoder(resp.Body).Decode(&value); err != nil {
		return nil, err
	}
	return value, nil
}

func (c Client) CreateSession(ctx context.Context, payload CreateSessionRequest) (CreateSessionResponse, error) {
	var out CreateSessionResponse
	if err := c.postJSON(ctx, "/v1/sessions", payload, &out); err != nil {
		return out, err
	}
	return out, nil
}

func (c Client) SendMessage(ctx context.Context, sessionID string, payload SendMessageRequest) (SendMessageResponse, error) {
	var out SendMessageResponse
	if err := c.postJSON(ctx, "/v1/sessions/"+sessionID+"/messages", payload, &out); err != nil {
		return out, err
	}
	return out, nil
}

func (c Client) Approve(ctx context.Context, sessionID string, approvalID string, acceptAll bool) error {
	return c.postJSON(ctx, "/v1/sessions/"+sessionID+"/approve", ApproveRequest{ApprovalID: approvalID, AcceptAll: acceptAll}, nil)
}

func (c Client) Reject(ctx context.Context, sessionID string, approvalID string) error {
	return c.postJSON(ctx, "/v1/sessions/"+sessionID+"/reject", ApprovalRequest{ApprovalID: approvalID}, nil)
}

func (c Client) StreamEvents(ctx context.Context, sessionID string, handle func(map[string]any) error) error {
	return c.streamEvents(ctx, sessionID, "", handle)
}

func (c Client) StreamRunEvents(ctx context.Context, sessionID string, runID string, handle func(map[string]any) error) error {
	return c.streamEvents(ctx, sessionID, runID, handle)
}

func (c Client) streamEvents(ctx context.Context, sessionID string, runID string, handle func(map[string]any) error) error {
	var lastEventID int64
	for {
		final, retryable, err := c.streamEventsOnce(ctx, sessionID, runID, lastEventID, handle, &lastEventID)
		if final {
			return nil
		}
		if !retryable {
			return err
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err := waitForReconnect(ctx); err != nil {
			return err
		}
	}
}

func (c Client) streamEventsOnce(
	ctx context.Context,
	sessionID string,
	runID string,
	after int64,
	handle func(map[string]any) error,
	lastEventID *int64,
) (bool, bool, error) {
	req, err := c.newStreamRequest(ctx, sessionID, runID, after)
	if err != nil {
		return false, false, err
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return false, true, err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return false, false, fmt.Errorf("event stream failed: %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	var pendingID int64
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "id:") {
			pendingID = parseEventID(strings.TrimSpace(strings.TrimPrefix(line, "id:")))
			continue
		}
		if !strings.HasPrefix(line, "data:") {
			continue
		}

		var event map[string]any
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if err := json.Unmarshal([]byte(payload), &event); err != nil {
			return false, false, err
		}
		if pendingID > 0 {
			*lastEventID = pendingID
		} else if payloadID := eventIDFromPayload(event); payloadID > 0 {
			*lastEventID = payloadID
		}
		pendingID = 0

		if err := handle(event); err != nil {
			return false, false, err
		}
		if event["type"] == "final" {
			return true, false, nil
		}
	}

	if err := scanner.Err(); err != nil {
		return false, true, err
	}
	return false, true, nil
}

func (c Client) newStreamRequest(ctx context.Context, sessionID string, runID string, after int64) (*http.Request, error) {
	streamURL := c.baseURL + "/v1/sessions/" + url.PathEscape(sessionID) + "/events"
	params := url.Values{}
	if after > 0 {
		params.Set("after", strconv.FormatInt(after, 10))
	}
	if runID != "" {
		params.Set("run_id", runID)
	}
	if encoded := params.Encode(); encoded != "" {
		streamURL += "?" + encoded
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, streamURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "text/event-stream")
	if after > 0 {
		req.Header.Set("Last-Event-ID", strconv.FormatInt(after, 10))
	}
	return req, nil
}

func waitForReconnect(ctx context.Context) error {
	timer := time.NewTimer(streamReconnectDelay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func parseEventID(raw string) int64 {
	id, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return 0
	}
	return id
}

func eventIDFromPayload(event map[string]any) int64 {
	switch value := event["event_id"].(type) {
	case float64:
		return int64(value)
	case int64:
		return value
	case int:
		return int64(value)
	case string:
		return parseEventID(value)
	default:
		return 0
	}
}

func (c Client) postJSON(ctx context.Context, path string, payload any, out any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	httpClient := c.http
	if _, ok := ctx.Deadline(); !ok {
		httpClient = &http.Client{Timeout: 15 * time.Second}
	}

	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("POST %s failed: %s: %s", path, resp.Status, strings.TrimSpace(string(body)))
	}

	if out == nil {
		io.Copy(io.Discard, resp.Body)
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}
