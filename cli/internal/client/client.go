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
}

type CreateSessionResponse struct {
	SessionID string `json:"session_id"`
}

type SendMessageRequest struct {
	Message   string `json:"message"`
	Mode      string `json:"mode"`
	Workspace string `json:"workspace"`
	Model     string `json:"model,omitempty"`
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

type SessionAgentStatus struct {
	Running        bool   `json:"running"`
	Queued         int    `json:"queued"`
	PendingSteers  int    `json:"pending_steers"`
	CurrentRunID   string `json:"current_run_id"`
	Stage          string `json:"stage"`
	StartedAt      string `json:"started_at"`
	LastProgressAt string `json:"last_progress_at"`
	ElapsedSeconds int    `json:"elapsed_seconds"`
	StalledSeconds int    `json:"stalled_seconds"`
}

type SessionResponse struct {
	SessionID string             `json:"session_id"`
	Workspace string             `json:"workspace"`
	CreatedAt string             `json:"created_at"`
	UpdatedAt string             `json:"updated_at"`
	Messages  []map[string]any   `json:"messages"`
	Approvals []map[string]any   `json:"approvals"`
	Agent     SessionAgentStatus `json:"agent"`
}

type SteerResponse struct {
	Status  string `json:"status"`
	RunID   string `json:"run_id"`
	Pending int    `json:"pending"`
}

type CompactResponse struct {
	Status     string         `json:"status"`
	Compaction map[string]any `json:"compaction"`
}

type ExecutionRequest struct {
	ExecutionID    string  `json:"execution_id"`
	Backend        string  `json:"backend"`
	Action         string  `json:"action"`
	Workspace      string  `json:"workspace"`
	TimeoutSeconds float64 `json:"timeout_seconds"`
}

type ExecutionResponse struct {
	ExecutionID string `json:"execution_id"`
	Backend     string `json:"backend"`
	Action      string `json:"action"`
	Status      string `json:"status"`
	ExitCode    int    `json:"exit_code"`
	Stdout      string `json:"stdout"`
	Stderr      string `json:"stderr"`
	DurationMS  int64  `json:"duration_ms"`
	TimedOut    bool   `json:"timed_out"`
	Cancelled   bool   `json:"cancelled"`
}

type CancelExecutionResponse struct {
	Status      string `json:"status"`
	ExecutionID string `json:"execution_id"`
}

type ContractTransport struct {
	Version     string `json:"version,omitempty"`
	EventSchema string `json:"event_schema,omitempty"`
	Status      string `json:"status"`
}

type ApplicationContract struct {
	Version string            `json:"version"`
	Schema  string            `json:"schema"`
	Types   map[string]string `json:"types"`
}

type APIContract struct {
	ContractVersion     string                       `json:"contract_version"`
	MinSupportedVersion string                       `json:"min_supported_version"`
	RuntimeVersion      string                       `json:"runtime_version"`
	Application         ApplicationContract          `json:"application"`
	Transports          map[string]ContractTransport `json:"transports"`
}

type TrustStatus struct {
	Workspace      string `json:"workspace"`
	Level          string `json:"level"`
	GitRemote      string `json:"git_remote"`
	RecordedRemote string `json:"recorded_remote"`
	Reason         string `json:"reason"`
	Removed        bool   `json:"removed,omitempty"`
}

type TrustListResponse struct {
	Projects []TrustStatus `json:"projects"`
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

func (c Client) Contract(ctx context.Context) (APIContract, error) {
	var out APIContract
	value, err := c.GetJSON(ctx, "/v1/meta/contract")
	if err != nil {
		return out, err
	}
	err = remarshalJSON(value, &out)
	return out, err
}

func (c Client) SendMessage(ctx context.Context, sessionID string, payload SendMessageRequest) (SendMessageResponse, error) {
	var out SendMessageResponse
	if err := c.postJSON(ctx, "/v1/sessions/"+url.PathEscape(sessionID)+"/messages", payload, &out); err != nil {
		return out, err
	}
	return out, nil
}

func (c Client) GetSession(ctx context.Context, sessionID string) (SessionResponse, error) {
	var out SessionResponse
	value, err := c.GetJSON(ctx, "/v1/sessions/"+url.PathEscape(sessionID))
	if err != nil {
		return out, err
	}
	err = remarshalJSON(value, &out)
	return out, err
}

func (c Client) LastSession(ctx context.Context) (SessionResponse, bool, error) {
	var out SessionResponse
	value, err := c.GetJSON(ctx, "/v1/sessions?last=true")
	if err != nil {
		return out, false, err
	}
	if value == nil {
		return out, false, nil
	}
	if err := remarshalJSON(value, &out); err != nil {
		return out, false, err
	}
	return out, true, nil
}

func (c Client) Steer(ctx context.Context, sessionID string, message string) (SteerResponse, error) {
	var out SteerResponse
	path := "/v1/sessions/" + url.PathEscape(sessionID) + "/steer"
	if err := c.postJSON(ctx, path, map[string]string{"message": message}, &out); err != nil {
		return out, err
	}
	return out, nil
}

func (c Client) Compact(ctx context.Context, sessionID string) (CompactResponse, error) {
	var out CompactResponse
	path := "/v1/sessions/" + url.PathEscape(sessionID) + "/compact"
	if err := c.postJSON(ctx, path, struct{}{}, &out); err != nil {
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

func (c Client) Execute(ctx context.Context, payload ExecutionRequest) (ExecutionResponse, error) {
	var out ExecutionResponse
	if err := c.postJSON(ctx, "/v1/executions", payload, &out); err != nil {
		return out, err
	}
	return out, nil
}

func (c Client) CancelExecution(ctx context.Context, executionID string) (CancelExecutionResponse, error) {
	var out CancelExecutionResponse
	path := "/v1/executions/" + url.PathEscape(executionID) + "/cancel"
	if err := c.postJSON(ctx, path, struct{}{}, &out); err != nil {
		return out, err
	}
	return out, nil
}

func (c Client) GetTrust(ctx context.Context, workspace string) (TrustStatus, error) {
	var out TrustStatus
	value, err := c.GetJSON(ctx, "/v1/trust?workspace="+url.QueryEscape(workspace))
	if err != nil {
		return out, err
	}
	err = remarshalJSON(value, &out)
	return out, err
}

func (c Client) ListTrust(ctx context.Context) (TrustListResponse, error) {
	var out TrustListResponse
	value, err := c.GetJSON(ctx, "/v1/trust")
	if err != nil {
		return out, err
	}
	err = remarshalJSON(value, &out)
	return out, err
}

func (c Client) TrustProject(ctx context.Context, workspace string) (TrustStatus, error) {
	var out TrustStatus
	err := c.postJSON(ctx, "/v1/trust", map[string]string{"workspace": workspace}, &out)
	return out, err
}

func (c Client) RemoveTrust(ctx context.Context, workspace string) (TrustStatus, error) {
	var out TrustStatus
	err := c.postJSON(ctx, "/v1/trust/remove", map[string]string{"workspace": workspace}, &out)
	return out, err
}

func (c Client) Approve(ctx context.Context, sessionID string, approvalID string, acceptAll bool) error {
	err := c.postJSON(ctx, "/v1/sessions/"+url.PathEscape(sessionID)+"/approve", ApproveRequest{ApprovalID: approvalID, AcceptAll: acceptAll}, nil)
	if isApprovalAlreadyResolved(err) {
		return nil
	}
	return err
}

func (c Client) Reject(ctx context.Context, sessionID string, approvalID string) error {
	err := c.postJSON(ctx, "/v1/sessions/"+url.PathEscape(sessionID)+"/reject", ApprovalRequest{ApprovalID: approvalID}, nil)
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

func remarshalJSON(value any, out any) error {
	content, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return json.Unmarshal(content, out)
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
			"%s failed: %s: %s\nRuntime authentication failed: the current CLI runtime.token does not match the running daemon. Run `aicode daemon stop`, make sure no stale uvicorn or daemon process owns port 8765, then run `aicode daemon start`.",
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
