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
	token   string
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

type CancelRunResponse struct {
	Status string  `json:"status"`
	RunID  *string `json:"run_id"`
	Queued int     `json:"queued"`
}

type ApprovalRequest struct {
	ApprovalID string `json:"approval_id"`
}

type ApproveRequest struct {
	ApprovalID string `json:"approval_id"`
	AcceptAll  bool   `json:"accept_all"`
}

func New(baseURL string, token string) Client {
	return Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		http: &http.Client{
			Timeout: 0,
		},
		token: token,
	}
}

func (c Client) setAuthHeader(req *http.Request) {
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
}

func (c Client) GetJSON(ctx context.Context, path string) (any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return nil, err
	}
	c.setAuthHeader(req)

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return nil, runtimeHTTPError("GET "+path, resp.StatusCode, resp.Status, body)
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

func (c Client) CancelRun(ctx context.Context, sessionID string) (CancelRunResponse, error) {
	var out CancelRunResponse
	if err := c.postJSON(ctx, "/v1/sessions/"+url.PathEscape(sessionID)+"/cancel", struct{}{}, &out); err != nil {
		return out, err
	}
	return out, nil
}

func (c Client) Approve(ctx context.Context, sessionID string, approvalID string, acceptAll bool) error {
	err := c.postJSON(ctx, "/v1/sessions/"+sessionID+"/approve", ApproveRequest{ApprovalID: approvalID, AcceptAll: acceptAll}, nil)
	if isApprovalAlreadyResolved(err) {
		return nil
	}
	return err
}

func (c Client) Reject(ctx context.Context, sessionID string, approvalID string) error {
	err := c.postJSON(ctx, "/v1/sessions/"+sessionID+"/reject", ApprovalRequest{ApprovalID: approvalID}, nil)
	if isApprovalAlreadyResolved(err) {
		return nil
	}
	return err
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
		return false, false, runtimeHTTPError("event stream", resp.StatusCode, resp.Status, body)
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
	c.setAuthHeader(req)
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
	c.setAuthHeader(req)

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
		return runtimeHTTPError("POST "+path, resp.StatusCode, resp.Status, body)
	}

	if out == nil {
		io.Copy(io.Discard, resp.Body)
		return nil
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

type RuntimeHTTPError struct {
	Operation  string
	StatusCode int
	Status     string
	Detail     string
}

func (err *RuntimeHTTPError) Error() string {
	if err.StatusCode == http.StatusUnauthorized {
		return fmt.Errorf(
			"%s failed: %s: %s\nRuntime 认证失败：当前 CLI 的 runtime.token 与正在运行的 daemon 不匹配。请运行 `aicode daemon stop`，确认 8765 端口没有旧 uvicorn/daemon 后，再 `aicode daemon start`。",
			err.Operation,
			err.Status,
			err.Detail,
		).Error()
	}
	return fmt.Sprintf("%s failed: %s: %s", err.Operation, err.Status, err.Detail)
}

func runtimeHTTPError(operation string, statusCode int, status string, body []byte) error {
	return &RuntimeHTTPError{
		Operation:  operation,
		StatusCode: statusCode,
		Status:     status,
		Detail:     strings.TrimSpace(string(body)),
	}
}

func isApprovalAlreadyResolved(err error) bool {
	httpErr, ok := err.(*RuntimeHTTPError)
	if !ok || httpErr.StatusCode != http.StatusNotFound {
		return false
	}
	return strings.Contains(httpErr.Detail, "approval not found or already resolved")
}
