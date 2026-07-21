// Package configcmd implements `aicode config ...` and `aicode review-rules`.
package configcmd

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

func Run(cfg config.Config, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("用法: aicode config <init|show|list|get|docs|set|unset|protected|review|test|workspace>")
	}

	switch args[0] {
	case "init":
		path, err := config.Init()
		if err != nil {
			return err
		}
		fmt.Printf("已创建配置文件: %s\n", path)
		return nil
	case "show":
		content, path, err := config.ReadRaw()
		if err != nil {
			return err
		}
		fmt.Printf("# %s\n%s", path, content)
		return nil
	case "list":
		if len(args) != 1 {
			return fmt.Errorf("用法: aicode config list")
		}
		return runConfigList(cfg)
	case "docs":
		if len(args) != 1 {
			return fmt.Errorf("用法: aicode config docs")
		}
		return runConfigDocs()
	case "get":
		if len(args) != 2 {
			return fmt.Errorf("用法: aicode config get <key>")
		}
		return runConfigGet(cfg, args[1])
	case "set":
		if len(args) != 3 {
			return fmt.Errorf("用法: aicode config set <key> <value>")
		}
		path, err := config.SetValue(args[1], args[2])
		if err != nil {
			return err
		}
		fmt.Printf("已更新 %s = %s (%s)\n", args[1], args[2], path)
		return nil
	case "unset":
		if len(args) != 2 {
			return fmt.Errorf("用法: aicode config unset <key>")
		}
		path, removed, err := config.UnsetValue(args[1])
		if err != nil {
			return err
		}
		if removed {
			fmt.Printf("已移除 %s (%s)\n", args[1], path)
			return nil
		}
		fmt.Printf("%s 未在用户配置中显式设置 (%s)\n", args[1], path)
		return nil
	case "protected":
		return runConfigProtectedCommand(args[1:])
	case "review":
		return runConfigReviewCommand(cfg, args[1:])
	case "test":
		return runConfigTestCommand(args[1:])
	case "workspace":
		return runConfigWorkspaceCommand(args[1:])
	default:
		return fmt.Errorf("未知 config 命令: %s", args[0])
	}
}

// ReviewRules implements the top-level `aicode review-rules` command.
func ReviewRules(cfg config.Config) error {
	value, err := fetchReviewRules(cfg)
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
}

func runConfigList(cfg config.Config) error {
	fmt.Println("Effective Config")
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "KEY\tVALUE")
	for _, entry := range cfg.Entries() {
		fmt.Fprintf(writer, "%s\t%s\n", entry.Key, entry.Value)
	}
	return writer.Flush()
}

func runConfigDocs() error {
	fmt.Println("Config Keys")
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "KEY\tDEFAULT\tENV\tDESCRIPTION")
	for _, doc := range config.KeyDocs() {
		fmt.Fprintf(writer, "%s\t%s\t%s\t%s\n", doc.Key, doc.Default, doc.Env, doc.Description)
	}
	return writer.Flush()
}

func runConfigGet(cfg config.Config, key string) error {
	value, ok := cfg.GetValue(key)
	if !ok {
		return fmt.Errorf("未知配置项: %s。运行 aicode config docs 查看支持列表", key)
	}
	fmt.Printf("%s = %s\n", key, value)
	return nil
}

func runConfigReviewCommand(cfg config.Config, args []string) error {
	if len(args) == 1 && args[0] == "list" {
		return runConfigReviewList(cfg)
	}
	if len(args) == 1 && args[0] == "docs" {
		return runConfigReviewDocs(cfg)
	}
	if len(args) == 1 && args[0] == "prune" {
		return runConfigReviewPrune(cfg)
	}
	if len(args) == 2 && args[0] == "unset" {
		return runConfigReviewUnset(args[1])
	}
	if len(args) == 3 && args[0] == "set" {
		return runConfigReviewSet(args[1], args[2])
	}
	if len(args) != 2 {
		return configReviewUsage()
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
		return configReviewUsage()
	}

	path, rules, err := projectconfig.SetReviewRuleDisabled(root.Path, rule, disabled, knownRules)
	if err != nil {
		return err
	}
	state := "启用"
	if disabled {
		state = "禁用"
	}
	fmt.Printf("已%s review 规则 %s (%s)\n", state, rule, path)
	if len(rules) == 0 {
		fmt.Println("当前 disabledRules: []")
		return nil
	}
	fmt.Printf("当前 disabledRules: %s\n", strings.Join(rules, ", "))
	return nil
}

func runConfigReviewSet(key string, rawValue string) error {
	value, err := strconv.Atoi(rawValue)
	if err != nil {
		return fmt.Errorf("%s 必须是整数: %w", key, err)
	}
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, field, saved, err := projectconfig.SetReviewNumber(root.Path, key, value)
	if err != nil {
		return err
	}
	fmt.Printf("已设置 review.%s = %d (%s)\n", field, saved, path)
	return nil
}

func runConfigReviewUnset(key string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, field, err := projectconfig.UnsetReviewNumber(root.Path, key)
	if err != nil {
		return err
	}
	fmt.Printf("已重置 review.%s 为默认值 (%s)\n", field, path)
	return nil
}

func runConfigReviewList(cfg config.Config) error {
	value, err := fetchReviewRules(cfg)
	if err != nil {
		return err
	}
	renderer.PrintReviewRulesTable(value)
	return nil
}

func runConfigReviewDocs(cfg config.Config) error {
	value, err := fetchReviewRules(cfg)
	if err != nil {
		return err
	}
	renderer.PrintReviewRulesMarkdown(value)
	return nil
}

func runConfigReviewPrune(cfg config.Config) error {
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
		fmt.Printf("未发现未知 review 规则 (%s)\n", path)
		return nil
	}
	fmt.Printf("已移除未知 review 规则: %s (%s)\n", strings.Join(removed, ", "), path)
	if len(rules) == 0 {
		fmt.Println("当前 disabledRules: []")
		return nil
	}
	fmt.Printf("当前 disabledRules: %s\n", strings.Join(rules, ", "))
	return nil
}

func configReviewUsage() error {
	return fmt.Errorf("用法: aicode config review <enable|disable> <rule_id> | set <largeDiffThreshold|maxFindings> <value> | unset <largeDiffThreshold|maxFindings> | list | docs | prune")
}

func runConfigProtectedCommand(args []string) error {
	if len(args) == 1 && args[0] == "list" {
		return runConfigProtectedList()
	}
	if len(args) == 2 && args[0] == "add" {
		return runConfigProtectedAdd(args[1])
	}
	if len(args) == 2 && (args[0] == "remove" || args[0] == "rm") {
		return runConfigProtectedRemove(args[1])
	}
	if len(args) == 1 && args[0] == "reset" {
		return runConfigProtectedReset()
	}
	return configProtectedUsage()
}

func runConfigProtectedList() error {
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

func runConfigProtectedAdd(pattern string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, values, err := projectconfig.AddProtectedPath(root.Path, pattern)
	if err != nil {
		return err
	}
	fmt.Printf("已添加 protected path %s (%s)\n", pattern, path)
	printStringList(values)
	return nil
}

func runConfigProtectedRemove(pattern string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, removed, values, err := projectconfig.RemoveProtectedPath(root.Path, pattern)
	if err != nil {
		return err
	}
	if removed {
		fmt.Printf("已移除 protected path %s (%s)\n", pattern, path)
	} else {
		fmt.Printf("未发现 protected path %s (%s)\n", pattern, path)
	}
	printStringList(values)
	return nil
}

func runConfigProtectedReset() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, values, err := projectconfig.ResetProtectedPaths(root.Path)
	if err != nil {
		return err
	}
	fmt.Printf("已重置 protectedPaths 为默认值 (%s)\n", path)
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

func configProtectedUsage() error {
	return fmt.Errorf("用法: aicode config protected add <pattern> | remove <pattern> | list | reset")
}

func runConfigTestCommand(args []string) error {
	if len(args) == 1 && (args[0] == "show" || args[0] == "get") {
		return runConfigTestShow()
	}
	if len(args) == 1 && args[0] == "auto" {
		return runConfigTestSet("auto")
	}
	if len(args) >= 2 && args[0] == "set" {
		return runConfigTestSet(strings.Join(args[1:], " "))
	}
	if len(args) == 1 && args[0] == "unset" {
		return runConfigTestUnset()
	}
	return configTestUsage()
}

func runConfigTestShow() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, command, configured, err := projectconfig.GetTestCommand(root.Path)
	if err != nil {
		return err
	}
	if !configured {
		fmt.Printf("commands.test 未配置，将自动探测 (%s)\n", path)
		return nil
	}
	fmt.Printf("commands.test = %s (%s)\n", command, path)
	return nil
}

func runConfigTestSet(command string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, saved, err := projectconfig.SetTestCommand(root.Path, command)
	if err != nil {
		return err
	}
	if saved == "auto" {
		fmt.Printf("已设置 commands.test = auto，Runtime 将自动探测测试命令 (%s)\n", path)
		return nil
	}
	fmt.Printf("已设置 commands.test = %s (%s)\n", saved, path)
	return nil
}

func runConfigTestUnset() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, removed, err := projectconfig.UnsetTestCommand(root.Path)
	if err != nil {
		return err
	}
	if removed {
		fmt.Printf("已移除 commands.test，Runtime 将自动探测测试命令 (%s)\n", path)
		return nil
	}
	fmt.Printf("commands.test 未在项目配置中显式设置 (%s)\n", path)
	return nil
}

func configTestUsage() error {
	return fmt.Errorf("用法: aicode config test set <command...> | auto | show | unset")
}

func runConfigWorkspaceCommand(args []string) error {
	if len(args) == 1 && args[0] == "list" {
		return runConfigWorkspaceList()
	}
	if len(args) == 3 && args[0] == "add" {
		return runConfigWorkspaceAdd(args[1], args[2])
	}
	if len(args) == 2 && (args[0] == "remove" || args[0] == "rm") {
		return runConfigWorkspaceRemove(args[1])
	}
	return configWorkspaceUsage()
}

func runConfigWorkspaceAdd(name string, targetPath string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, entries, err := projectconfig.SetWorkspace(root.Path, name, targetPath)
	if err != nil {
		return err
	}
	fmt.Printf("已添加只读 workspace %s -> %s (%s)\n", name, targetPath, path)
	printWorkspaceEntries(entries)
	return nil
}

func runConfigWorkspaceRemove(name string) error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, removed, entries, err := projectconfig.RemoveWorkspace(root.Path, name)
	if err != nil {
		return err
	}
	if removed {
		fmt.Printf("已移除 workspace %s (%s)\n", name, path)
	} else {
		fmt.Printf("未发现 workspace %s (%s)\n", name, path)
	}
	printWorkspaceEntries(entries)
	return nil
}

func runConfigWorkspaceList() error {
	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	path, entries, err := projectconfig.ListWorkspaces(root.Path)
	if err != nil {
		return err
	}
	if len(entries) == 0 {
		fmt.Printf("未配置额外 workspace (%s)\n", path)
		return nil
	}
	fmt.Printf("Project workspaces (%s)\n", path)
	printWorkspaceEntries(entries)
	return nil
}

func printWorkspaceEntries(entries []projectconfig.WorkspaceEntry) {
	if len(entries) == 0 {
		fmt.Println("当前 workspaces: []")
		return
	}
	writer := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(writer, "NAME\tPATH\tMODE")
	for _, entry := range entries {
		fmt.Fprintf(writer, "%s\t%s\t%s\n", entry.Name, entry.Path, entry.Mode)
	}
	writer.Flush()
}

func configWorkspaceUsage() error {
	return fmt.Errorf("用法: aicode config workspace add <name> <path> | remove <name> | list")
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
