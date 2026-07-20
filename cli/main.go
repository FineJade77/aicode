package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
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

	options, commandArgs, err := parseGlobalArgs(args)
	if err != nil {
		return err
	}
	if options.Sandbox != "" {
		return runSandboxCommand(options.Sandbox, commandArgs)
	}
	args = commandArgs

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
  aicode --sandbox docker test
  aicode sessions
  aicode resume --last
  aicode resume --last "继续刚才的任务"
  aicode resume <session_id> "继续这个会话"
  aicode usage [--json]
  aicode usage --today [--json]
  aicode usage --session <session_id> [--json]
  aicode models [--json]
  aicode config init
  aicode config show
  aicode config list
  aicode config docs
  aicode config get models.reviewer
  aicode config set ui.language en-US
  aicode config set models.reviewer gpt-5
  aicode config set models.main gpt-5
  aicode config set provider.type anthropic
  aicode config set provider.anthropic.timeout_seconds 120
  aicode config unset models.reviewer
  aicode config protected add secrets/local/**
  aicode config protected list
  aicode config protected remove secrets/local/**
  aicode config protected reset
  aicode config review disable large_diff
  aicode config review enable large_diff
  aicode config review set largeDiffThreshold 1200
  aicode config review unset largeDiffThreshold
  aicode config review list
  aicode config review docs
  aicode config review prune
  aicode config test set python3 -m pytest
  aicode config test auto
  aicode config test show
  aicode config test unset
  aicode config workspace add api ../api
  aicode config workspace list
  aicode config workspace remove api
  aicode daemon start
  aicode daemon stop
  aicode daemon status`)
}

type globalOptions struct {
	Sandbox string
}

func parseGlobalArgs(args []string) (globalOptions, []string, error) {
	options := globalOptions{}
	for len(args) > 0 {
		switch args[0] {
		case "--sandbox":
			if len(args) < 2 || strings.TrimSpace(args[1]) == "" {
				return options, nil, fmt.Errorf("用法: aicode --sandbox docker test")
			}
			options.Sandbox = args[1]
			args = args[2:]
		default:
			return options, args, nil
		}
	}
	return options, args, nil
}

func runSandboxCommand(sandbox string, args []string) error {
	if sandbox != "docker" {
		return fmt.Errorf("暂只支持: aicode --sandbox docker test")
	}
	if len(args) != 1 || args[0] != "test" {
		return fmt.Errorf("用法: aicode --sandbox docker test")
	}
	return runDockerSandboxTest()
}

func runDockerSandboxTest() error {
	if _, err := exec.LookPath("docker"); err != nil {
		return fmt.Errorf("docker 未安装或不在 PATH: %w", err)
	}

	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	command, err := detectSandboxTestCommand(root.Path)
	if err != nil {
		return err
	}
	image := dockerSandboxImage(command)
	fmt.Printf("Sandbox: docker\nWorkspace: %s\nImage: %s\nCommand: %s\n", root.Path, image, command)

	cmd := exec.Command("docker", dockerSandboxArgs(root.Path, image, command)...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func detectSandboxTestCommand(workspacePath string) (string, error) {
	_, command, configured, err := projectconfig.GetTestCommand(workspacePath)
	if err != nil {
		return "", err
	}
	if configured && command != "auto" {
		return command, nil
	}

	if fileExists(workspacePath, "go.mod") {
		return "go test ./...", nil
	}
	if fileExists(workspacePath, "go.work") {
		modules, err := parseGoWorkModules(workspacePath)
		if err != nil {
			return "", err
		}
		if len(modules) > 0 {
			packages := make([]string, 0, len(modules))
			for _, module := range modules {
				packages = append(packages, module+"/...")
			}
			return "go test " + strings.Join(packages, " "), nil
		}
	}
	if fileExists(workspacePath, "pyproject.toml") || fileExists(workspacePath, "pytest.ini") || fileExists(workspacePath, "setup.cfg") {
		return "python3 -m pytest", nil
	}
	if fileExists(workspacePath, "package.json") {
		if command := detectPackageTestCommand(workspacePath); command != "" {
			return command, nil
		}
	}
	return "", fmt.Errorf("未能自动探测测试命令，请先运行 aicode config test set <command...>")
}

func detectPackageTestCommand(workspacePath string) string {
	content, err := os.ReadFile(filepath.Join(workspacePath, "package.json"))
	if err == nil {
		var payload map[string]any
		if json.Unmarshal(content, &payload) == nil {
			if scripts, ok := payload["scripts"].(map[string]any); ok {
				if _, ok := scripts["test"]; !ok {
					return ""
				}
			}
		}
	}
	if fileExists(workspacePath, "pnpm-lock.yaml") {
		return "pnpm test"
	}
	if fileExists(workspacePath, "yarn.lock") {
		return "yarn test"
	}
	return "npm test"
}

func parseGoWorkModules(workspacePath string) ([]string, error) {
	content, err := os.ReadFile(filepath.Join(workspacePath, "go.work"))
	if err != nil {
		return nil, err
	}
	modules := []string{}
	inUseBlock := false
	for _, raw := range strings.Split(string(content), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "//") {
			continue
		}
		if line == "use (" {
			inUseBlock = true
			continue
		}
		if inUseBlock && line == ")" {
			inUseBlock = false
			continue
		}
		if strings.HasPrefix(line, "use ") {
			module := strings.TrimSpace(strings.TrimPrefix(line, "use "))
			if strings.HasPrefix(module, "./") {
				modules = append(modules, module)
			}
			continue
		}
		if inUseBlock && strings.HasPrefix(line, "./") {
			modules = append(modules, line)
		}
	}
	return modules, nil
}

func dockerSandboxImage(command string) string {
	if image := strings.TrimSpace(os.Getenv("AICODE_SANDBOX_DOCKER_IMAGE")); image != "" {
		return image
	}
	fields := strings.Fields(command)
	if len(fields) == 0 {
		return "ubuntu:24.04"
	}
	switch fields[0] {
	case "go":
		return "golang:1.22"
	case "node", "npm", "pnpm", "yarn":
		return "node:22"
	case "python", "python3", "pytest":
		return "python:3.12-slim"
	default:
		return "ubuntu:24.04"
	}
}

func dockerSandboxArgs(workspacePath string, image string, command string) []string {
	return []string{
		"run",
		"--rm",
		"--network",
		"none",
		"--env",
		"AICODE_SANDBOX=1",
		"--mount",
		"type=bind,src=" + workspacePath + ",dst=/workspace,readonly",
		"-w",
		"/workspace",
		image,
		"sh",
		"-lc",
		command,
	}
}

func fileExists(basePath string, name string) bool {
	_, err := os.Stat(filepath.Join(basePath, name))
	return err == nil
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
		renderer.PrintDaemonStatus(status)
		return nil
	default:
		return fmt.Errorf("未知 daemon 命令: %s", args[0])
	}
}

func runConfigCommand(cfg config.Config, args []string) error {
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

func runResume(cfg config.Config, args []string) error {
	target, err := parseResumeArgs(args)
	if err != nil {
		return err
	}

	if target.Inspect && target.UseLast {
		return runSimpleGet(cfg, "/v1/sessions?last=true")
	}
	if target.Inspect {
		return runSimpleGet(cfg, "/v1/sessions/"+url.PathEscape(target.SessionID))
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
	Language  string
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
	return fmt.Errorf("用法: aicode resume <session_id> [message] 或 aicode resume --last [message]")
}

func fetchResumeSession(cfg config.Config, target resumeTarget) (any, error) {
	if target.UseLast {
		return fetchRuntimeJSON(cfg, "/v1/sessions?last=true")
	}
	return fetchRuntimeJSON(cfg, "/v1/sessions/"+url.PathEscape(target.SessionID))
}

func sessionInfoFromValue(value any) (sessionInfo, error) {
	if value == nil {
		return sessionInfo{}, fmt.Errorf("没有可恢复的 session")
	}
	payload, ok := value.(map[string]any)
	if !ok {
		return sessionInfo{}, fmt.Errorf("session 响应格式无效")
	}
	sessionID := strings.TrimSpace(stringValueFromMap(payload, "session_id"))
	workspacePath := strings.TrimSpace(stringValueFromMap(payload, "workspace"))
	language := strings.TrimSpace(stringValueFromMap(payload, "language"))
	if sessionID == "" {
		return sessionInfo{}, fmt.Errorf("session 缺少 session_id")
	}
	if workspacePath == "" {
		return sessionInfo{}, fmt.Errorf("session 缺少 workspace")
	}
	if language == "" {
		language = "zh-CN"
	}
	return sessionInfo{SessionID: sessionID, Workspace: workspacePath, Language: language}, nil
}

func stringValueFromMap(payload map[string]any, key string) string {
	value, _ := payload[key].(string)
	return value
}

func runResumeAgent(cfg config.Config, session sessionInfo, message string) error {
	if strings.TrimSpace(message) == "" {
		return resumeUsage()
	}
	if err := ensureDaemon(cfg); err != nil {
		return err
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	api := client.New(cfg.Runtime.URL, daemon.Token())
	run, err := api.SendMessage(ctx, session.SessionID, client.SendMessageRequest{
		Message:   message,
		Mode:      "chat",
		Workspace: session.Workspace,
		Language:  session.Language,
	})
	if err != nil {
		return err
	}

	fmt.Printf("恢复会话: %s\n", session.SessionID)
	fmt.Printf("工作区: %s\n", session.Workspace)
	return api.StreamRunEvents(ctx, session.SessionID, run.RunID, func(event map[string]any) error {
		renderer.RenderEvent(event)
		return handleInteractiveEvent(api, session.SessionID, event)
	})
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

	api := client.New(cfg.Runtime.URL, daemon.Token())
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

	api := client.New(cfg.Runtime.URL, daemon.Token())
	session, err := api.CreateSession(ctx, client.CreateSessionRequest{
		Workspace: root.Path,
		Language:  cfg.UI.Language,
	})
	if err != nil {
		return err
	}

	run, err := api.SendMessage(ctx, session.SessionID, client.SendMessageRequest{
		Message:   prompt,
		Mode:      mode,
		Workspace: root.Path,
		Language:  cfg.UI.Language,
	})
	if err != nil {
		return err
	}

	fmt.Printf("会话: %s\n", session.SessionID)
	return api.StreamRunEvents(ctx, session.SessionID, run.RunID, func(event map[string]any) error {
		renderer.RenderEvent(event)
		return handleInteractiveEvent(api, session.SessionID, event)
	})
}

func handleInteractiveEvent(api client.Client, sessionID string, event map[string]any) error {
	eventType, _ := event["type"].(string)
	switch eventType {
	case "approval.requested":
		approvalID, _ := event["approval_id"].(string)
		if approvalID == "" {
			return fmt.Errorf("approval.requested 缺少 approval_id")
		}
		if kind, _ := event["kind"].(string); kind == "edit" {
			if diff, _ := event["diff"].(string); diff != "" {
				fmt.Println(diff)
			}
			return resolveEditApproval(api, sessionID, approvalID)
		}
		return resolveApprovalWithPrompt(api, sessionID, approvalID, "允许执行这个工具操作吗？输入 y 确认，其它任意输入拒绝 [y/N]: ")
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

	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()

	if strings.EqualFold(strings.TrimSpace(answer), "y") {
		return api.Approve(ctx, sessionID, approvalID, false)
	}
	return api.Reject(ctx, sessionID, approvalID)
}

func resolveEditApproval(api client.Client, sessionID string, approvalID string) error {
	fmt.Print("应用这个编辑吗？[y=应用 / a=应用并允许本会话后续编辑 / 其它=拒绝]: ")
	reader := bufio.NewReader(os.Stdin)
	line, _ := reader.ReadString('\n')
	answer := strings.ToLower(strings.TrimSpace(line))
	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()
	switch answer {
	case "y", "yes":
		return api.Approve(ctx, sessionID, approvalID, false)
	case "a", "all":
		return api.Approve(ctx, sessionID, approvalID, true)
	default:
		return api.Reject(ctx, sessionID, approvalID)
	}
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
