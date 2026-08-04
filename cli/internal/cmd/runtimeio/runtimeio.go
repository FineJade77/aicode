package runtimeio

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/renderer"
)

const DefaultTimeout = 10 * time.Second

func EnsureDaemon(cfg config.Config) error {
	ctx, cancel := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel()
	if _, err := daemon.Status(ctx, cfg.Runtime.URL); err == nil {
		return nil
	}

	fmt.Println("Runtime daemon is not running; starting it...")
	if err := daemon.Start(cfg); err != nil {
		return err
	}
	return daemon.WaitUntilReady(cfg.Runtime.URL, 5*time.Second)
}

func FetchJSON(cfg config.Config, path string) (any, error) {
	return FetchJSONWithTimeout(cfg, path, DefaultTimeout)
}

func FetchJSONWithTimeout(cfg config.Config, path string, timeout time.Duration) (any, error) {
	if err := EnsureDaemon(cfg); err != nil {
		return nil, err
	}

	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL, daemon.Token())
	return api.GetJSON(ctx, path)
}

func RunSimpleGet(cfg config.Config, path string) error {
	value, err := FetchJSON(cfg, path)
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
}

// StreamAndHandle streams run events to the terminal and resolves any
// interactive approval prompts raised along the way.
func StreamAndHandle(ctx context.Context, api client.Client, sessionID string, runID string) error {
	// The individual failures are printed as they happen, scattered through
	// whatever tool output came after them. The tracker replays them at the end
	// as one account, so a run that stopped for a reason does not have to be
	// reconstructed by scrolling.
	tracker := renderer.NewRunTracker()
	err := api.StreamRunEvents(ctx, sessionID, runID, func(event map[string]any) error {
		renderer.RenderEvent(event)
		tracker.Observe(event)
		return handleInteractiveEvent(api, sessionID, event)
	})
	tracker.PrintSummaryTo(os.Stdout)
	return err
}

func handleInteractiveEvent(api client.Client, sessionID string, event map[string]any) error {
	eventType, _ := event["type"].(string)
	switch eventType {
	case "approval.requested":
		approvalID, _ := event["approval_id"].(string)
		if approvalID == "" {
			return fmt.Errorf("approval.requested is missing approval_id")
		}
		if kind, _ := event["kind"].(string); kind == "edit" {
			paths := stringList(event["paths"])
			if len(paths) > 1 {
				return resolveBatchEditApproval(api, sessionID, approvalID, event, paths)
			}
			if diff, _ := event["diff"].(string); diff != "" {
				fmt.Println(diff)
			}
			return resolveEditApproval(api, sessionID, approvalID)
		}
		return resolveApprovalWithPrompt(api, sessionID, approvalID, "Allow this tool operation? Enter y to approve; any other input denies [y/N]: ")
	default:
		return nil
	}
}

func resolveApprovalWithPrompt(api client.Client, sessionID string, approvalID string, prompt string) error {
	fmt.Print(prompt)
	reader := bufio.NewReader(os.Stdin)
	answer, err := reader.ReadString('\n')
	if err != nil && len(answer) == 0 {
		answer = "n"
	}

	ctx, cancel := context.WithTimeout(context.Background(), DefaultTimeout)
	defer cancel()

	if strings.EqualFold(strings.TrimSpace(answer), "y") {
		return api.Approve(ctx, sessionID, approvalID, false)
	}
	return api.Reject(ctx, sessionID, approvalID)
}

func resolveEditApproval(api client.Client, sessionID string, approvalID string) error {
	fmt.Print("Apply this edit? [y=apply / a=apply and allow later edits in this session / other=deny]: ")
	reader := bufio.NewReader(os.Stdin)
	line, _ := reader.ReadString('\n')
	answer := strings.ToLower(strings.TrimSpace(line))
	ctx, cancel := context.WithTimeout(context.Background(), DefaultTimeout)
	defer cancel()
	switch answer {
	case "y", "yes":
		return api.Approve(ctx, sessionID, approvalID, false)
	case "a", "all":
		return api.Approve(ctx, sessionID, approvalID, true)
	case "r", "revise":
		return api.RejectWithGuidance(ctx, sessionID, approvalID, readGuidance())
	default:
		return api.Reject(ctx, sessionID, approvalID)
	}
}

// readGuidance collects the "do it this way instead" text.
//
// Empty input falls back to a plain refusal rather than sending an empty
// revision: telling the model the user explained something when they did not is
// a lie it will then try to act on.
func readGuidance() string {
	fmt.Print("What should be done differently? ")
	reader := bufio.NewReader(os.Stdin)
	line, _ := reader.ReadString('\n')
	return strings.TrimSpace(line)
}

// resolveBatchEditApproval asks about several files at once.
//
// Every diff is printed before the prompt, because deciding on the first file
// without having seen the second is the thing this replaces.
func resolveBatchEditApproval(api client.Client, sessionID string, approvalID string, event map[string]any, paths []string) error {
	items, _ := event["items"].([]any)
	for index, item := range items {
		entry, ok := item.(map[string]any)
		if !ok {
			continue
		}
		fmt.Printf("\n[%d] %v\n", index+1, entry["path"])
		if diff, _ := entry["diff"].(string); diff != "" {
			fmt.Println(diff)
		}
	}
	fmt.Printf(
		"\nApply these %d edits? [y=all / a=all and allow later edits / r=revise / 1,3=only those / other=deny]: ",
		len(paths),
	)
	reader := bufio.NewReader(os.Stdin)
	line, _ := reader.ReadString('\n')
	answer := strings.ToLower(strings.TrimSpace(line))

	ctx, cancel := context.WithTimeout(context.Background(), DefaultTimeout)
	defer cancel()
	switch answer {
	case "y", "yes":
		return api.Approve(ctx, sessionID, approvalID, false)
	case "a", "all":
		return api.Approve(ctx, sessionID, approvalID, true)
	case "r", "revise":
		return api.RejectWithGuidance(ctx, sessionID, approvalID, readGuidance())
	}
	if selection := selectedPaths(answer, paths); len(selection) > 0 {
		return api.ApproveSelection(ctx, sessionID, approvalID, false, selection)
	}
	return api.Reject(ctx, sessionID, approvalID)
}

// selectedPaths maps "1,3" onto the paths those numbers refer to.
//
// An index that does not exist is dropped rather than shifting the rest: an
// off-by-one that silently applies the wrong file is worse than a selection the
// user has to retype.
func selectedPaths(answer string, paths []string) []string {
	var selection []string
	for _, field := range strings.FieldsFunc(answer, func(r rune) bool { return r == ',' || r == ' ' }) {
		index, err := strconv.Atoi(strings.TrimSpace(field))
		if err != nil || index < 1 || index > len(paths) {
			return nil
		}
		selection = append(selection, paths[index-1])
	}
	return selection
}

func stringList(value any) []string {
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if text, ok := item.(string); ok {
			out = append(out, text)
		}
	}
	return out
}
