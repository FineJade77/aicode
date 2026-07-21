// Package sandboxcmd implements `aicode --sandbox docker <test|build|lint>`.
package sandboxcmd

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/projectconfig"
	"github.com/FineJade77/aicode/cli/internal/workspace"
)

func Run(sandbox string, args []string) error {
	if sandbox != "docker" {
		return fmt.Errorf("暂只支持: aicode --sandbox docker <test|build|lint>")
	}
	if len(args) != 1 || !supportedSandboxAction(args[0]) {
		return fmt.Errorf("用法: aicode --sandbox docker <test|build|lint>")
	}
	return runDockerSandbox(args[0])
}

func runDockerSandboxTest() error {
	return runDockerSandbox("test")
}

func runDockerSandbox(action string) error {
	if _, err := exec.LookPath("docker"); err != nil {
		return fmt.Errorf("docker 未安装或不在 PATH: %w", err)
	}

	root, err := workspace.Detect()
	if err != nil {
		return err
	}
	command, err := detectSandboxCommand(root.Path, action)
	if err != nil {
		return err
	}
	image := dockerSandboxImage(command)
	limits := defaultDockerSandboxLimits()
	fmt.Printf(
		"Sandbox: docker\nAction: %s\nWorkspace: %s\nImage: %s\nCommand: %s\nLimits: cpus=%s memory=%s pids=%s\n",
		action,
		root.Path,
		image,
		command,
		limits.CPUs,
		limits.Memory,
		limits.PIDsLimit,
	)

	envMasks, cleanup, err := dockerSandboxEnvMasks(root.Path)
	if err != nil {
		return err
	}
	defer cleanup()

	auditSandboxEvent("sandbox.started", root.Path, sandboxAuditData(action, image, command, limits, len(envMasks), nil))
	started := time.Now()
	cmd := exec.Command("docker", dockerSandboxArgs(root.Path, image, command, envMasks, limits)...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	runErr := cmd.Run()
	result := map[string]any{
		"duration_ms": time.Since(started).Milliseconds(),
		"exit_code":   sandboxExitCode(runErr),
		"success":     runErr == nil,
	}
	if runErr != nil {
		result["error"] = runErr.Error()
	}
	auditSandboxEvent("sandbox.finished", root.Path, sandboxAuditData(action, image, command, limits, len(envMasks), result))
	return runErr
}

func supportedSandboxAction(action string) bool {
	switch action {
	case "test", "build", "lint":
		return true
	default:
		return false
	}
}

func detectSandboxTestCommand(workspacePath string) (string, error) {
	return detectSandboxCommand(workspacePath, "test")
}

func detectSandboxCommand(workspacePath string, action string) (string, error) {
	_, command, configured, err := projectconfig.GetCommand(workspacePath, action)
	if err != nil {
		return "", err
	}
	if configured && command != "auto" {
		return command, nil
	}
	return detectAutoSandboxCommand(workspacePath, action)
}

func detectAutoSandboxCommand(workspacePath string, action string) (string, error) {
	if !supportedSandboxAction(action) {
		return "", fmt.Errorf("不支持的 sandbox action: %s", action)
	}
	if fileExists(workspacePath, "package.json") {
		if command := detectPackageScriptCommand(workspacePath, action); command != "" {
			return command, nil
		}
	}
	if fileExists(workspacePath, "go.mod") {
		return goSandboxCommand(action, "./...")
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
			return goSandboxCommand(action, strings.Join(packages, " "))
		}
	}
	if action == "test" && (fileExists(workspacePath, "pyproject.toml") || fileExists(workspacePath, "pytest.ini") || fileExists(workspacePath, "setup.cfg")) {
		return "python3 -m pytest", nil
	}
	if action == "lint" && detectRuffConfig(workspacePath) {
		return "python3 -m ruff check .", nil
	}
	return "", fmt.Errorf("未能自动探测 %s 命令，请在 .aicode/config.json 的 commands.%s 中配置命令或 auto", action, action)
}

func detectPackageTestCommand(workspacePath string) string {
	return detectPackageScriptCommand(workspacePath, "test")
}

func detectPackageScriptCommand(workspacePath string, script string) string {
	content, err := os.ReadFile(filepath.Join(workspacePath, "package.json"))
	if err == nil {
		var payload map[string]any
		if json.Unmarshal(content, &payload) == nil {
			if scripts, ok := payload["scripts"].(map[string]any); ok {
				if _, ok := scripts[script]; !ok {
					return ""
				}
			}
		}
	}
	if fileExists(workspacePath, "pnpm-lock.yaml") {
		return "pnpm " + script
	}
	if fileExists(workspacePath, "yarn.lock") {
		return "yarn " + script
	}
	if script == "test" {
		return "npm test"
	}
	return "npm run " + script
}

func goSandboxCommand(action string, packages string) (string, error) {
	switch action {
	case "test":
		return "go test " + packages, nil
	case "build":
		return "go build " + packages, nil
	case "lint":
		return "go vet " + packages, nil
	default:
		return "", fmt.Errorf("不支持的 sandbox action: %s", action)
	}
}

func detectRuffConfig(workspacePath string) bool {
	for _, name := range []string{"ruff.toml", ".ruff.toml"} {
		if fileExists(workspacePath, name) {
			return true
		}
	}
	content, err := os.ReadFile(filepath.Join(workspacePath, "pyproject.toml"))
	return err == nil && strings.Contains(string(content), "[tool.ruff")
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

type sandboxMount struct {
	Source string
	Target string
}

type dockerSandboxLimits struct {
	CPUs      string
	Memory    string
	PIDsLimit string
}

func defaultDockerSandboxLimits() dockerSandboxLimits {
	return dockerSandboxLimits{
		CPUs:      sandboxLimitEnv("AICODE_SANDBOX_CPUS", "2"),
		Memory:    sandboxLimitEnv("AICODE_SANDBOX_MEMORY", "2g"),
		PIDsLimit: sandboxLimitEnv("AICODE_SANDBOX_PIDS_LIMIT", "256"),
	}
}

func sandboxLimitEnv(name string, fallback string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	return value
}

func dockerSandboxEnvMasks(workspacePath string) ([]sandboxMount, func(), error) {
	maskedFiles, err := sandboxEnvFiles(workspacePath)
	if err != nil {
		return nil, nil, err
	}
	if len(maskedFiles) == 0 {
		return nil, func() {}, nil
	}
	tempDir, err := os.MkdirTemp("", "aicode-sandbox-mask-*")
	if err != nil {
		return nil, nil, fmt.Errorf("创建 sandbox env mask 失败: %w", err)
	}
	cleanup := func() {
		_ = os.RemoveAll(tempDir)
	}
	emptyFile := filepath.Join(tempDir, "empty-env")
	if err := os.WriteFile(emptyFile, []byte{}, 0o600); err != nil {
		cleanup()
		return nil, nil, fmt.Errorf("创建 sandbox env mask 文件失败: %w", err)
	}

	mounts := make([]sandboxMount, 0, len(maskedFiles))
	for _, name := range maskedFiles {
		mounts = append(mounts, sandboxMount{
			Source: emptyFile,
			Target: filepath.ToSlash(filepath.Join("/workspace", name)),
		})
	}
	return mounts, cleanup, nil
}

func sandboxEnvFiles(workspacePath string) ([]string, error) {
	matches, err := filepath.Glob(filepath.Join(workspacePath, ".env*"))
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(matches))
	for _, match := range matches {
		info, err := os.Stat(match)
		if err != nil || info.IsDir() || !info.Mode().IsRegular() {
			continue
		}
		names = append(names, filepath.Base(match))
	}
	sort.Strings(names)
	return names, nil
}

func dockerSandboxArgs(workspacePath string, image string, command string, envMasks []sandboxMount, limits dockerSandboxLimits) []string {
	args := []string{
		"run",
		"--rm",
		"--network",
		"none",
		"--cpus",
		limits.CPUs,
		"--memory",
		limits.Memory,
		"--pids-limit",
		limits.PIDsLimit,
		"--env",
		"AICODE_SANDBOX=1",
		"--env",
		"HOME=/tmp/aicode-home",
		"--env",
		"XDG_CACHE_HOME=/tmp/aicode-cache",
		"--env",
		"GOCACHE=/tmp/aicode-go-build",
		"--env",
		"GOMODCACHE=/tmp/aicode-go-mod",
		"--env",
		"npm_config_cache=/tmp/aicode-npm-cache",
		"--env",
		"YARN_CACHE_FOLDER=/tmp/aicode-yarn-cache",
		"--env",
		"PIP_CACHE_DIR=/tmp/aicode-pip-cache",
		"--mount",
		"type=bind,src=" + workspacePath + ",dst=/workspace,readonly",
	}
	for _, mask := range envMasks {
		args = append(args, "--mount", "type=bind,src="+mask.Source+",dst="+mask.Target+",readonly")
	}
	args = append(args,
		"-w",
		"/workspace",
		image,
		"sh",
		"-lc",
		command,
	)
	return args
}

func sandboxExitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		return exitError.ExitCode()
	}
	return -1
}

func sandboxAuditData(action string, image string, command string, limits dockerSandboxLimits, envMaskCount int, result map[string]any) map[string]any {
	hash := sha256.Sum256([]byte(command))
	data := map[string]any{
		"backend":        "docker",
		"action":         action,
		"image":          image,
		"command_hash":   fmt.Sprintf("%x", hash[:]),
		"network":        "none",
		"workspace_mode": "read_only",
		"env_files":      "masked",
		"env_mask_count": envMaskCount,
		"cpus":           limits.CPUs,
		"memory":         limits.Memory,
		"pids_limit":     limits.PIDsLimit,
	}
	for key, value := range result {
		data[key] = value
	}
	return data
}

func auditSandboxEvent(eventType string, workspacePath string, data map[string]any) {
	if err := appendAuditEvent(eventType, workspacePath, data); err != nil {
		fmt.Fprintf(os.Stderr, "警告: sandbox 审计日志写入失败: %v\n", err)
	}
}

func appendAuditEvent(eventType string, workspacePath string, data map[string]any) error {
	home, err := config.Home()
	if err != nil {
		return err
	}
	event := map[string]any{
		"timestamp":  time.Now().UTC().Format(time.RFC3339Nano),
		"event_type": eventType,
		"session_id": nil,
		"workspace":  workspacePath,
		"data":       data,
	}
	content, err := json.Marshal(event)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(home, 0o700); err != nil {
		return err
	}
	path := filepath.Join(home, "audit.jsonl")
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := file.Write(append(content, '\n')); err != nil {
		return err
	}
	return nil
}

func fileExists(basePath string, name string) bool {
	_, err := os.Stat(filepath.Join(basePath, name))
	return err == nil
}
