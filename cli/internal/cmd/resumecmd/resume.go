// Package resumecmd implements `aicode resume ...`.
package resumecmd

import (
	"context"
	"fmt"
	"net/url"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
)

func Run(cfg config.Config, args []string) error {
	target, err := parseResumeArgs(args)
	if err != nil {
		return err
	}

	if target.Inspect && target.UseLast {
		return runtimeio.RunSimpleGet(cfg, "/v1/sessions?last=true")
	}
	if target.Inspect {
		return runtimeio.RunSimpleGet(cfg, "/v1/sessions/"+url.PathEscape(target.SessionID))
	}

	sessionValue, err := fetchResumeSession(cfg, target)
	if err != nil {
		return err
	}
	session, err := sessionInfoFromValue(sessionValue)
	if err != nil {
		return err
	}
	return runResumeAgent(cfg, session, target.Message)
}

type resumeTarget struct {
	SessionID string
	UseLast   bool
	Message   string
	Inspect   bool
}

type sessionInfo struct {
	SessionID string
	Workspace string
}

func parseResumeArgs(args []string) (resumeTarget, error) {
	if len(args) == 0 {
		return resumeTarget{}, resumeUsage()
	}
	if args[0] == "--last" {
		if len(args) == 1 {
			return resumeTarget{UseLast: true, Inspect: true}, nil
		}
		return resumeTarget{UseLast: true, Message: strings.Join(args[1:], " ")}, nil
	}
	if len(args) == 1 {
		return resumeTarget{SessionID: args[0], Inspect: true}, nil
	}
	return resumeTarget{SessionID: args[0], Message: strings.Join(args[1:], " ")}, nil
}

func resumeUsage() error {
	return fmt.Errorf("usage: aicode resume <session_id> [message] or aicode resume --last [message]")
}

func fetchResumeSession(cfg config.Config, target resumeTarget) (any, error) {
	if target.UseLast {
		return runtimeio.FetchJSON(cfg, "/v1/sessions?last=true")
	}
	return runtimeio.FetchJSON(cfg, "/v1/sessions/"+url.PathEscape(target.SessionID))
}

func sessionInfoFromValue(value any) (sessionInfo, error) {
	if value == nil {
		return sessionInfo{}, fmt.Errorf("no session is available to resume")
	}
	payload, ok := value.(map[string]any)
	if !ok {
		return sessionInfo{}, fmt.Errorf("invalid session response")
	}
	sessionID := strings.TrimSpace(stringValueFromMap(payload, "session_id"))
	workspacePath := strings.TrimSpace(stringValueFromMap(payload, "workspace"))
	if sessionID == "" {
		return sessionInfo{}, fmt.Errorf("session is missing session_id")
	}
	if workspacePath == "" {
		return sessionInfo{}, fmt.Errorf("session is missing workspace")
	}
	return sessionInfo{SessionID: sessionID, Workspace: workspacePath}, nil
}

func stringValueFromMap(payload map[string]any, key string) string {
	value, _ := payload[key].(string)
	return value
}

func runResumeAgent(cfg config.Config, session sessionInfo, message string) error {
	if strings.TrimSpace(message) == "" {
		return resumeUsage()
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	api := client.New(cfg.Runtime.URL, daemon.Token())
	run, err := api.SendMessage(ctx, session.SessionID, client.SendMessageRequest{
		Message:   message,
		Mode:      "chat",
		Workspace: session.Workspace,
		Language:  config.DefaultLanguage,
	})
	if err != nil {
		return err
	}

	fmt.Printf("Resumed session: %s\n", session.SessionID)
	fmt.Printf("Workspace: %s\n", session.Workspace)
	return runtimeio.StreamAndHandle(ctx, api, session.SessionID, run.RunID)
}
