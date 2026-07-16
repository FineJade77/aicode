package main

import (
	"bufio"
	"context"
	"fmt"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/aicode-dev/aicode/cli/internal/client"
	"github.com/aicode-dev/aicode/cli/internal/config"
	"github.com/aicode-dev/aicode/cli/internal/daemon"
	"github.com/aicode-dev/aicode/cli/internal/projectconfig"
	"github.com/aicode-dev/aicode/cli/internal/renderer"
	"github.com/aicode-dev/aicode/cli/internal/workspace"
)

const defaultTimeout = 10 * time.Second

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "错误: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	if len(args) == 0 {
		printHelp()
		return nil
	}

	switch args[0] {
	case "daemon":
		return runDaemonCommand(cfg, args[1:])
	case "config":
		return runConfigCommand(cfg, args[1:])
	case "sessions":
		return runSimpleGet(cfg, "/v1/sessions")
	case "usage":
		return runUsage(cfg, args[1:])
	case "models":
		return runModels(cfg, args[1:])
	case "review-rules":
		return runReviewRules(cfg)
	case "resume":
		return runResume(cfg, args[1:])
	case "review":
		return runAgent(cfg, "review", "请审查当前代码变更。")
	case "diff":
		return runAgent(cfg, "diff", "请查看当前 git diff 并总结变更。")
	case "test":
		return runAgent(cfg, "test", "请自动发现并运行当前项目的低风险测试命令。")
	case "explain":
		if len(args) < 2 {
			return fmt.Errorf("用法: aicode explain <file-or-symbol>")
		}
		return runAgent(cfg, "explain", "请解释 "+strings.Join(args[1:], " "))
	case "chat":
		if len(args) < 2 {
			return fmt.Errorf("用法: aicode chat <message>")
		}
		return runAgent(cfg, "chat", strings.Join(args[1:], " "))
	default:
		return runAgent(cfg, "default", strings.Join(args, " "))
	}
}

func printHelp() {
	fmt.Println(`aicode - 本地优先的 CLI Coding Agent

用法:
  aicode "修复这个测试失败"
  aicode chat "解释当前目录"
  aicode review
  aicode review-rules
  aicode explain src/foo.ts
  aicode diff
  aicode test
  aicode sessions
  aicode resume --last
  aicode usage [--json]
  aicode usage --today [--json]
  aicode usage --session <session_id> [--json]
  aicode models [--json]
  aicode config init
  aicode config show
  aicode config list
  aicode config get models.reviewer
  aicode config set ui.language en-US
  aicode config set models.reviewer gpt-5
  aicode config unset models.reviewer
  aicode config review disable large_diff
  aicode config review enable large_diff
  aicode config review set largeDiffThreshold 1200
  aicode config review unset largeDiffThreshold
  aicode config review list
  aicode config review docs
  aicode config review prune
  aicode daemon start
  aicode daemon stop
  aicode daemon status`)
}

func runDaemonCommand(cfg config.Config, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("用法: aicode daemon <start|stop|status>")
	}

	switch args[0] {
	case "start":
		if err := daemon.Start(cfg); err != nil {
			return err
		}
		fmt.Println("Runtime daemon 已启动。")
		return nil
	case "stop":
		if err := daemon.Stop(); err != nil {
			return err
		}
		fmt.Println("Runtime daemon 已停止。")
		return nil
	case "status":
		ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
		defer cancel()
		status, err := daemon.Status(ctx, cfg.Runtime.URL)
		if err != nil {
			return err
		}
		renderer.PrintJSON(status)
		return nil
	default:
		return fmt.Errorf("未知 daemon 命令: %s", args[0])
	}
}

func runConfigCommand(cfg config.Config, args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("用法: aicode config <init|show|list|get|set|unset|review>")
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
	case "review":
		return runConfigReviewCommand(cfg, args[1:])
	default:
		return fmt.Errorf("未知 config 命令: %s", args[0])
	}
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

func runConfigGet(cfg config.Config, key string) error {
	value, ok := cfg.GetValue(key)
	if !ok {
		return fmt.Errorf("未知配置项: %s。运行 aicode config list 查看支持列表", key)
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

func runResume(cfg config.Config, args []string) error {
	if len(args) == 1 && args[0] == "--last" {
		return runSimpleGet(cfg, "/v1/sessions?last=true")
	}
	if len(args) != 1 {
		return fmt.Errorf("用法: aicode resume <session_id> 或 aicode resume --last")
	}
	return runSimpleGet(cfg, "/v1/sessions/"+args[0])
}

func runUsage(cfg config.Config, args []string) error {
	path, jsonOutput, err := usagePath(args)
	if err != nil {
		return err
	}
	value, err := fetchRuntimeJSON(cfg, path)
	if err != nil {
		return err
	}
	if jsonOutput {
		renderer.PrintJSON(value)
		return nil
	}
	renderer.PrintUsageSummary(value)
	return nil
}

func usagePath(args []string) (string, bool, error) {
	jsonOutput := false
	filtered := make([]string, 0, len(args))
	for _, arg := range args {
		if arg == "--json" {
			jsonOutput = true
			continue
		}
		filtered = append(filtered, arg)
	}
	if len(filtered) == 0 {
		return "/v1/usage", jsonOutput, nil
	}
	if len(filtered) == 1 && filtered[0] == "--today" {
		return "/v1/usage?today=true", jsonOutput, nil
	}
	if len(filtered) == 2 && filtered[0] == "--session" {
		return "/v1/usage/sessions/" + filtered[1], jsonOutput, nil
	}
	return "", false, fmt.Errorf("用法: aicode usage [--today|--session <session_id>] [--json]")
}

func runModels(cfg config.Config, args []string) error {
	jsonOutput := false
	if len(args) == 1 && args[0] == "--json" {
		jsonOutput = true
	} else if len(args) != 0 {
		return fmt.Errorf("用法: aicode models [--json]")
	}

	value, err := fetchRuntimeJSON(cfg, "/v1/models/routes")
	if err != nil {
		return err
	}
	if jsonOutput {
		renderer.PrintJSON(value)
		return nil
	}
	renderer.PrintModelRoutes(value)
	return nil
}

func runReviewRules(cfg config.Config) error {
	value, err := fetchReviewRules(cfg)
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
}

func fetchReviewRules(cfg config.Config) (any, error) {
	root, err := workspace.Detect()
	if err != nil {
		return nil, err
	}
	return fetchReviewRulesForWorkspace(cfg, root.Path)
}

func fetchReviewRulesForWorkspace(cfg config.Config, workspacePath string) (any, error) {
	if err := ensureDaemon(cfg); err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL)
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

func runSimpleGet(cfg config.Config, path string) error {
	value, err := fetchRuntimeJSON(cfg, path)
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
}

func fetchRuntimeJSON(cfg config.Config, path string) (any, error) {
	if err := ensureDaemon(cfg); err != nil {
		return nil, err
	}

	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL)
	return api.GetJSON(ctx, path)
}

func runAgent(cfg config.Config, mode string, prompt string) error {
	if strings.TrimSpace(prompt) == "" {
		return fmt.Errorf("请输入任务内容")
	}
	if err := ensureDaemon(cfg); err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	root, err := workspace.Detect()
	if err != nil {
		return err
	}

	api := client.New(cfg.Runtime.URL)
	session, err := api.CreateSession(ctx, client.CreateSessionRequest{
		Workspace: root.Path,
		Language:  cfg.UI.Language,
	})
	if err != nil {
		return err
	}

	if err := api.SendMessage(ctx, session.SessionID, client.SendMessageRequest{
		Message:   prompt,
		Mode:      mode,
		Workspace: root.Path,
		Language:  cfg.UI.Language,
	}); err != nil {
		return err
	}

	fmt.Printf("会话: %s\n", session.SessionID)
	return api.StreamEvents(ctx, session.SessionID, func(event map[string]any) error {
		renderer.RenderEvent(event)
		return handleInteractiveEvent(api, session.SessionID, event)
	})
}

func handleInteractiveEvent(api client.Client, sessionID string, event map[string]any) error {
	eventType, _ := event["type"].(string)
	if eventType != "patch.preview" {
		return nil
	}

	approvalID, _ := event["approval_id"].(string)
	if approvalID == "" {
		return fmt.Errorf("patch.preview 缺少 approval_id")
	}

	fmt.Print("应用这个 patch 吗？输入 y 确认，其它任意输入拒绝 [y/N]: ")
	reader := bufio.NewReader(os.Stdin)
	answer, err := reader.ReadString('\n')
	if err != nil && len(answer) == 0 {
		answer = "n"
	}

	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()

	if strings.EqualFold(strings.TrimSpace(answer), "y") {
		return api.Approve(ctx, sessionID, approvalID)
	}
	return api.Reject(ctx, sessionID, approvalID)
}

func ensureDaemon(cfg config.Config) error {
	ctx, cancel := context.WithTimeout(context.Background(), 700*time.Millisecond)
	defer cancel()
	if _, err := daemon.Status(ctx, cfg.Runtime.URL); err == nil {
		return nil
	}

	fmt.Println("Runtime daemon 未运行，正在启动...")
	if err := daemon.Start(cfg); err != nil {
		return err
	}
	return daemon.WaitUntilReady(cfg.Runtime.URL, 5*time.Second)
}
