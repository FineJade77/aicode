package projectcmd

import (
	"context"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"

	"github.com/FineJade77/aicode/cli/internal/client"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/projectconfig"
	"github.com/FineJade77/aicode/cli/internal/renderer"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

func PrintReviewRulesJSON(cfg config.Config) error {
	value, err := fetchReviewRules(cfg)
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
}

func runReview(cfg config.Config, args []string) error {
	if len(args) == 1 && args[0] == "list" {
		return runReviewList(cfg)
	}
	if len(args) == 1 && args[0] == "docs" {
		return runReviewDocs(cfg)
	}
	if len(args) == 1 && args[0] == "prune" {
		return runReviewPrune(cfg)
	}
	if len(args) == 2 && args[0] == "unset" {
		return runReviewUnset(args[1])
	}
	if len(args) == 3 && args[0] == "set" {
		return runReviewSet(args[1], args[2])
	}
	if len(args) != 2 {
		return reviewUsage()
	}

	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	ruleData, err := fetchReviewRulesForWorkspace(cfg, root.Path)
	if err != nil {
		return err
	}
	knownRules := reviewRuleIDs(ruleData)

	action := args[0]
	rule := args[1]
	var disabled bool
	switch action {
	case "disable":
		disabled = true
	case "enable":
		disabled = false
	default:
		return reviewUsage()
	}

	path, rules, err := projectconfig.SetReviewRuleDisabled(root.Path, rule, disabled, knownRules)
	if err != nil {
		return err
	}
	state := "Enabled"
	if disabled {
		state = "Disabled"
	}
	fmt.Printf("%s review rule %s (%s)\n", state, rule, path)
	if len(rules) == 0 {
		fmt.Println("Current disabledRules: []")
		return nil
	}
	fmt.Printf("Current disabledRules: %s\n", strings.Join(rules, ", "))
	return nil
}

func runReviewSet(key string, rawValue string) error {
	value, err := strconv.Atoi(rawValue)
	if err != nil {
		return fmt.Errorf("%s must be an integer: %w", key, err)
	}
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, field, saved, err := projectconfig.SetReviewNumber(root.Path, key, value)
	if err != nil {
		return err
	}
	fmt.Printf("Set review.%s = %d (%s)\n", field, saved, path)
	return nil
}

func runReviewUnset(key string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, field, err := projectconfig.UnsetReviewNumber(root.Path, key)
	if err != nil {
		return err
	}
	fmt.Printf("Reset review.%s to its default (%s)\n", field, path)
	return nil
}

func runReviewList(cfg config.Config) error {
	value, err := fetchReviewRules(cfg)
	if err != nil {
		return err
	}
	renderer.PrintReviewRulesTable(value)
	return nil
}

func runReviewDocs(cfg config.Config) error {
	value, err := fetchReviewRules(cfg)
	if err != nil {
		return err
	}
	renderer.PrintReviewRulesMarkdown(value)
	return nil
}

func runReviewPrune(cfg config.Config) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	ruleData, err := fetchReviewRulesForWorkspace(cfg, root.Path)
	if err != nil {
		return err
	}

	path, removed, rules, err := projectconfig.PruneUnknownReviewRules(root.Path, reviewRuleIDs(ruleData))
	if err != nil {
		return err
	}
	if len(removed) == 0 {
		fmt.Printf("No unknown review rules found (%s)\n", path)
		return nil
	}
	fmt.Printf("Removed unknown review rules: %s (%s)\n", strings.Join(removed, ", "), path)
	if len(rules) == 0 {
		fmt.Println("Current disabledRules: []")
		return nil
	}
	fmt.Printf("Current disabledRules: %s\n", strings.Join(rules, ", "))
	return nil
}

func reviewUsage() error {
	return fmt.Errorf("usage: aicode project review <enable|disable> <rule_id> | set <largeDiffThreshold|maxFindings> <value> | unset <largeDiffThreshold|maxFindings> | list | docs | prune")
}

func runProtected(args []string) error {
	if len(args) == 1 && args[0] == "list" {
		return runProtectedList()
	}
	if len(args) == 2 && args[0] == "add" {
		return runProtectedAdd(args[1])
	}
	if len(args) == 2 && (args[0] == "remove" || args[0] == "rm") {
		return runProtectedRemove(args[1])
	}
	if len(args) == 1 && args[0] == "reset" {
		return runProtectedReset()
	}
	return protectedUsage()
}

func runProtectedList() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, values, explicit, err := projectconfig.ListProtectedPaths(root.Path)
	if err != nil {
		return err
	}
	source := "project config"
	if !explicit {
		source = "defaults"
	}
	fmt.Printf("Protected paths (%s, %s)\n", source, path)
	printStringList(values)
	return nil
}

func runProtectedAdd(pattern string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, values, err := projectconfig.AddProtectedPath(root.Path, pattern)
	if err != nil {
		return err
	}
	fmt.Printf("Added protected path %s (%s)\n", pattern, path)
	printStringList(values)
	return nil
}

func runProtectedRemove(pattern string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, removed, values, err := projectconfig.RemoveProtectedPath(root.Path, pattern)
	if err != nil {
		return err
	}
	if removed {
		fmt.Printf("Removed protected path %s (%s)\n", pattern, path)
	} else {
		fmt.Printf("Protected path %s was not found (%s)\n", pattern, path)
	}
	printStringList(values)
	return nil
}

func runProtectedReset() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, values, err := projectconfig.ResetProtectedPaths(root.Path)
	if err != nil {
		return err
	}
	fmt.Printf("Reset protectedPaths to defaults (%s)\n", path)
	printStringList(values)
	return nil
}

func printStringList(values []string) {
	if len(values) == 0 {
		fmt.Println("[]")
		return
	}
	for _, value := range values {
		fmt.Printf("- %s\n", value)
	}
}

func protectedUsage() error {
	return fmt.Errorf("usage: aicode project protected add <pattern> | remove <pattern> | list | reset")
}

func runTestCommand(args []string) error {
	if len(args) == 1 && (args[0] == "show" || args[0] == "get") {
		return runTestCommandShow()
	}
	if len(args) == 1 && args[0] == "auto" {
		return runTestCommandSet("auto")
	}
	if len(args) >= 2 && args[0] == "set" {
		return runTestCommandSet(strings.Join(args[1:], " "))
	}
	if len(args) == 1 && args[0] == "unset" {
		return runTestCommandUnset()
	}
	return testCommandUsage()
}

func runTestCommandShow() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, command, configured, err := projectconfig.GetTestCommand(root.Path)
	if err != nil {
		return err
	}
	if !configured {
		fmt.Printf("commands.test is not configured and will be auto-detected (%s)\n", path)
		return nil
	}
	fmt.Printf("commands.test = %s (%s)\n", command, path)
	return nil
}

func runTestCommandSet(command string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, saved, err := projectconfig.SetTestCommand(root.Path, command)
	if err != nil {
		return err
	}
	if saved == "auto" {
		fmt.Printf("Set commands.test = auto; Runtime will auto-detect the test command (%s)\n", path)
		return nil
	}
	fmt.Printf("Set commands.test = %s (%s)\n", saved, path)
	return nil
}

func runTestCommandUnset() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, removed, err := projectconfig.UnsetTestCommand(root.Path)
	if err != nil {
		return err
	}
	if removed {
		fmt.Printf("Removed commands.test; Runtime will auto-detect the test command (%s)\n", path)
		return nil
	}
	fmt.Printf("commands.test is not explicitly set in the project configuration (%s)\n", path)
	return nil
}

func testCommandUsage() error {
	return fmt.Errorf("usage: aicode project command test set <command...> | auto | show | unset")
}

func runWorkspace(args []string) error {
	if len(args) == 1 && args[0] == "list" {
		return runWorkspaceList()
	}
	if len(args) == 3 && args[0] == "add" {
		return runWorkspaceAdd(args[1], args[2])
	}
	if len(args) == 2 && (args[0] == "remove" || args[0] == "rm") {
		return runWorkspaceRemove(args[1])
	}
	return workspaceUsage()
}

func runWorkspaceAdd(name string, targetPath string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, entries, err := projectconfig.SetWorkspace(root.Path, name, targetPath)
	if err != nil {
		return err
	}
	fmt.Printf("Added read-only workspace %s -> %s (%s)\n", name, targetPath, path)
	printWorkspaceEntries(entries)
	return nil
}

func runWorkspaceRemove(name string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, removed, entries, err := projectconfig.RemoveWorkspace(root.Path, name)
	if err != nil {
		return err
	}
	if removed {
		fmt.Printf("Removed workspace %s (%s)\n", name, path)
	} else {
		fmt.Printf("Workspace %s was not found (%s)\n", name, path)
	}
	printWorkspaceEntries(entries)
	return nil
}

func runWorkspaceList() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, entries, err := projectconfig.ListWorkspaces(root.Path)
	if err != nil {
		return err
	}
	if len(entries) == 0 {
		fmt.Printf("No additional workspaces are configured (%s)\n", path)
		return nil
	}
	fmt.Printf("Project workspaces (%s)\n", path)
	printWorkspaceEntries(entries)
	return nil
}

func printWorkspaceEntries(entries []projectconfig.WorkspaceEntry) {
	if len(entries) == 0 {
		fmt.Println("Current workspaces: []")
		return
	}
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "NAME\tPATH\tMODE")
	for _, entry := range entries {
		fmt.Fprintf(writer, "%s\t%s\t%s\n", entry.Name, entry.Path, entry.Mode)
	}
	writer.Flush()
}

func workspaceUsage() error {
	return fmt.Errorf("usage: aicode project workspace add <name> <path> | remove <name> | list")
}

func fetchReviewRules(cfg config.Config) (any, error) {
	root, err := workspace.Detect()
	if err != nil {
		return nil, err
	}
	return fetchReviewRulesForWorkspace(cfg, root.Path)
}

func fetchReviewRulesForWorkspace(cfg config.Config, workspacePath string) (any, error) {
	if err := runtimeio.EnsureDaemon(cfg); err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(context.Background(), runtimeio.DefaultTimeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL, daemon.Token())
	return api.GetJSON(ctx, "/v1/review/rules?workspace="+url.QueryEscape(workspacePath))
}

func reviewRuleIDs(value any) []string {
	payload, ok := value.(map[string]any)
	if !ok {
		return nil
	}
	rawRules, ok := payload["rules"].([]any)
	if !ok {
		return nil
	}

	seen := map[string]bool{}
	ids := make([]string, 0, len(rawRules))
	for _, item := range rawRules {
		rule, ok := item.(map[string]any)
		if !ok {
			continue
		}
		id, ok := rule["id"].(string)
		id = strings.TrimSpace(id)
		if !ok || id == "" || seen[id] {
			continue
		}
		seen[id] = true
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
