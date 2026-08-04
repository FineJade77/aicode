package projectcmd

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

const sandboxTimeout = 30 * time.Minute

func runSandbox(cfg config.Config, sandbox string, args []string) error {
	if sandbox != "docker" {
		return fmt.Errorf("only the docker sandbox is currently supported")
	}
	action, artifacts, err := parseSandboxArgs(args)
	if err != nil {
		return err
	}
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return err
	}

	executionID, err := newExecutionID()
	if err != nil {
		return err
	}
	api := client.New(cfg.Runtime.URL, daemon.Token())
	signalContext, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancelTimeout := context.WithTimeout(signalContext, sandboxTimeout+30*time.Second)
	defer cancelTimeout()

	response, err := api.Execute(ctx, client.ExecutionRequest{
		ExecutionID:    executionID,
		Backend:        "docker",
		Action:         action,
		Workspace:      root.Path,
		TimeoutSeconds: sandboxTimeout.Seconds(),
		Artifacts:      artifacts,
	})
	if signalContext.Err() != nil {
		cancelContext, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_, _ = api.CancelExecution(cancelContext, executionID)
		return fmt.Errorf("sandbox execution was cancelled")
	}
	if err != nil {
		return err
	}
	renderExecution(response)
	if response.Status != "succeeded" {
		return fmt.Errorf(
			"sandbox execution %s (exit=%d, status=%s)",
			response.ExecutionID,
			response.ExitCode,
			response.Status,
		)
	}
	return nil
}

func supportedSandboxAction(action string) bool {
	switch action {
	case "test", "build", "lint":
		return true
	default:
		return false
	}
}

func renderExecution(response client.ExecutionResponse) {
	fmt.Printf(
		"Sandbox: %s\nAction: %s\nExecution: %s\nStatus: %s\nExit: %d\nDuration: %dms\n",
		response.Backend,
		response.Action,
		response.ExecutionID,
		response.Status,
		response.ExitCode,
		response.DurationMS,
	)
	if output := strings.TrimRight(response.Stdout, "\n"); output != "" {
		fmt.Println(output)
	}
	if output := strings.TrimRight(response.Stderr, "\n"); output != "" {
		fmt.Fprintln(os.Stderr, output)
	}
	renderArtifacts(response)
}

// renderArtifacts lists what the run exported, with digests.
//
// The digest is the point: an exported report is only worth anything if it can
// be tied back to the run that produced it. A truncated set says so rather than
// looking complete — a missing test report reads as a test that never ran.
func renderArtifacts(response client.ExecutionResponse) {
	if len(response.Artifacts) == 0 && !response.ArtifactsTruncated {
		return
	}
	fmt.Printf("\nArtifacts (%d):\n", len(response.Artifacts))
	for _, artifact := range response.Artifacts {
		fmt.Printf("  %s  %d bytes  sha256:%s\n", artifact.Path, artifact.SizeBytes, artifact.SHA256)
	}
	if response.ArtifactsTruncated {
		fmt.Println("  (truncated: some files were skipped for being too large, too many, or not regular files)")
	}
}

func newExecutionID() (string, error) {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("failed to generate execution id: %w", err)
	}
	return "exec_" + hex.EncodeToString(raw), nil
}

// parseSandboxArgs reads the action and the opt-in artifact flag.
//
// Opt-in rather than always-on: without it the container has no writable path
// at all, and that is the right default for a command whose product is its exit
// code. Turning it on adds exactly one writable directory, outside the
// workspace, so a build can emit reports without the repository itself becoming
// writable to model-chosen commands.
func parseSandboxArgs(args []string) (string, bool, error) {
	action := ""
	artifacts := false
	for _, arg := range args {
		switch {
		case arg == "--artifacts":
			artifacts = true
		case action == "" && supportedSandboxAction(arg):
			action = arg
		default:
			return "", false, fmt.Errorf("usage: aicode project sandbox <test|build|lint> [--artifacts]")
		}
	}
	if action == "" {
		return "", false, fmt.Errorf("usage: aicode project sandbox <test|build|lint> [--artifacts]")
	}
	return action, artifacts, nil
}
