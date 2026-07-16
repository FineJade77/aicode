package client

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	baseURL string
	http    *http.Client
}

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

func (c Client) SendMessage(ctx context.Context, sessionID string, payload SendMessageRequest) error {
	return c.postJSON(ctx, "/v1/sessions/"+sessionID+"/messages", payload, nil)
}

func (c Client) StreamEvents(ctx context.Context, sessionID string, handle func(map[string]any)) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/v1/sessions/"+sessionID+"/events", nil)
	if err != nil {
		return err
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("event stream failed: %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}

		var event map[string]any
		if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &event); err != nil {
			return err
		}
		handle(event)
		if event["type"] == "final" {
			return nil
		}
	}

	return scanner.Err()
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
