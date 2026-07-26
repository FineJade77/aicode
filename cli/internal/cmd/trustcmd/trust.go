// Package trustcmd manages repository-external project trust records.
package trustcmd

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"text/tabwriter"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

func Run(cfg config.Config, args []string) error {
	action, jsonOutput, err := parseArgs(args)
	if err != nil {
		return err
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}
	api := client.New(cfg.Runtime.URL, daemon.Token())
	ctx, cancel := context.WithTimeout(context.Background(), runtimeio.DefaultTimeout)
	defer cancel()

	if action == "list" {
		response, err := api.ListTrust(ctx)
		if err != nil {
			return err
		}
		return renderList(response, jsonOutput)
	}

	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	var status client.TrustStatus
	switch action {
	case "status":
		status, err = api.GetTrust(ctx, root.Path)
	case "add":
		status, err = api.TrustProject(ctx, root.Path)
	case "remove":
		status, err = api.RemoveTrust(ctx, root.Path)
	}
	if err != nil {
		return err
	}
	return renderStatus(status, jsonOutput)
}

func parseArgs(args []string) (string, bool, error) {
	jsonOutput := false
	filtered := make([]string, 0, len(args))
	for _, arg := range args {
		if arg == "--json" {
			jsonOutput = true
		} else {
			filtered = append(filtered, arg)
		}
	}
	if len(filtered) > 1 {
		return "", false, trustUsage()
	}
	action := "status"
	if len(filtered) == 1 {
		action = filtered[0]
	}
	switch action {
	case "status", "add", "remove", "list":
		return action, jsonOutput, nil
	default:
		return "", false, trustUsage()
	}
}

func renderStatus(status client.TrustStatus, jsonOutput bool) error {
	if jsonOutput {
		return json.NewEncoder(os.Stdout).Encode(status)
	}
	fmt.Printf("Workspace: %s\nTrust: %s\nReason: %s\n", status.Workspace, status.Level, status.Reason)
	if status.GitRemote != "" {
		fmt.Printf("Git remote: %s\n", status.GitRemote)
	}
	return nil
}

func renderList(response client.TrustListResponse, jsonOutput bool) error {
	if jsonOutput {
		return json.NewEncoder(os.Stdout).Encode(response)
	}
	if len(response.Projects) == 0 {
		fmt.Println("没有已信任的 project。")
		return nil
	}
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "WORKSPACE\tLEVEL\tGIT REMOTE")
	for _, project := range response.Projects {
		fmt.Fprintf(writer, "%s\t%s\t%s\n", project.Workspace, project.Level, project.GitRemote)
	}
	return writer.Flush()
}

func trustUsage() error {
	return fmt.Errorf("用法: aicode trust [status|add|remove|list] [--json]")
}
