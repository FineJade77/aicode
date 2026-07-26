// Package doctorcmd implements the read-only `aicode doctor` diagnostics.
package doctorcmd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/FineJade77/aicode/cli/internal/config"
	"github.com/FineJade77/aicode/cli/internal/daemon"
	"github.com/FineJade77/aicode/cli/internal/version"
)

const (
	StatusOK    = "ok"
	StatusWarn  = "warn"
	StatusError = "error"
)

var ErrChecksFailed = errors.New("doctor 检查发现错误，请按提示修复")

type Check struct {
	Name        string         `json:"name"`
	Status      string         `json:"status"`
	Summary     string         `json:"summary"`
	Details     map[string]any `json:"details,omitempty"`
	Remediation string         `json:"remediation,omitempty"`
}

type Report struct {
	Status     string  `json:"status"`
	CLIVersion string  `json:"cli_version"`
	Checks     []Check `json:"checks"`
}

type dependencies struct {
	resolveRuntime func() (daemon.RuntimeInstallation, error)
	daemonStatus   func(context.Context, string) (map[string]any, error)
	runCommand     func(context.Context, string, ...string) (string, error)
	lookPath       func(string) (string, error)
	portOpen       func(string, time.Duration) (bool, error)
	lookupEnv      func(string) (string, bool)
	cliVersion     string
}

type runtimeState struct {
	installation daemon.RuntimeInstallation
	installErr   error
	daemonStatus map[string]any
	daemonErr    error
}

func Run(cfg config.Config, args []string) error {
	jsonOutput, err := parseArgs(args)
	if err != nil {
		return err
	}
	report := buildReport(cfg, realDependencies())
	if jsonOutput {
		err = renderJSON(os.Stdout, report)
	} else {
		err = renderHuman(os.Stdout, report)
	}
	if err != nil {
		return err
	}
	if report.Status == StatusError {
		return ErrChecksFailed
	}
	return nil
}

func parseArgs(args []string) (bool, error) {
	jsonOutput := false
	for _, arg := range args {
		if arg != "--json" {
			return false, fmt.Errorf("用法: aicode doctor [--json]")
		}
		jsonOutput = true
	}
	return jsonOutput, nil
}

func realDependencies() dependencies {
	return dependencies{
		resolveRuntime: daemon.ResolveRuntime,
		daemonStatus:   daemon.Status,
		runCommand: func(ctx context.Context, name string, args ...string) (string, error) {
			output, err := exec.CommandContext(ctx, name, args...).CombinedOutput()
			return strings.TrimSpace(string(output)), err
		},
		lookPath: exec.LookPath,
		portOpen: func(address string, timeout time.Duration) (bool, error) {
			connection, err := net.DialTimeout("tcp", address, timeout)
			if err != nil {
				return false, err
			}
			_ = connection.Close()
			return true, nil
		},
		lookupEnv:  os.LookupEnv,
		cliVersion: version.Current(),
	}
}

func buildReport(cfg config.Config, deps dependencies) Report {
	state := runtimeState{}
	state.installation, state.installErr = deps.resolveRuntime()

	statusContext, cancelStatus := context.WithTimeout(context.Background(), 800*time.Millisecond)
	state.daemonStatus, state.daemonErr = deps.daemonStatus(statusContext, cfg.Runtime.URL)
	cancelStatus()

	checks := []Check{
		checkInstallation(state),
		checkVersions(state, deps.cliVersion),
		checkPython(state, deps),
		checkPort(cfg, state, deps),
		checkProvider(cfg, deps),
		checkDocker(deps),
	}
	return Report{
		Status:     aggregateStatus(checks),
		CLIVersion: normalizedVersion(deps.cliVersion),
		Checks:     checks,
	}
}

func checkInstallation(state runtimeState) Check {
	if state.installErr != nil {
		return Check{
			Name:        "installation",
			Status:      StatusError,
			Summary:     "无法解析 Runtime 安装",
			Details:     map[string]any{"error": state.installErr.Error()},
			Remediation: "运行 `make install`，或为源码开发设置有效的 AICODE_RUNTIME_DIR。",
		}
	}
	return Check{
		Name:    "installation",
		Status:  StatusOK,
		Summary: "Runtime 安装布局可用",
		Details: map[string]any{
			"python":      state.installation.Python,
			"runtime_dir": state.installation.RuntimeDir,
			"source":      state.installation.Source,
			"version":     state.installation.Version,
		},
	}
}

func checkVersions(state runtimeState, cliVersion string) Check {
	cliVersion = normalizedVersion(cliVersion)
	details := map[string]any{"cli": cliVersion}
	if state.installErr != nil {
		return Check{
			Name:        "version",
			Status:      StatusError,
			Summary:     "无法验证 CLI/Runtime 版本",
			Details:     details,
			Remediation: "先修复 Runtime 安装，再重新运行 `aicode doctor`。",
		}
	}

	runtimeVersion := strings.TrimSpace(state.installation.Version)
	details["runtime"] = runtimeVersion
	daemonVersion := stringDetail(state.daemonStatus, "version")
	if daemonVersion != "" {
		details["daemon"] = daemonVersion
	}

	if runtimeVersion != "" && daemonVersion != "" && runtimeVersion != daemonVersion {
		return versionMismatch(details, "已安装 Runtime 与运行中 daemon 版本不一致")
	}
	if cliVersion != "dev" {
		if runtimeVersion != "" && cliVersion != runtimeVersion {
			return versionMismatch(details, "CLI 与已安装 Runtime 版本不一致")
		}
		if daemonVersion != "" && cliVersion != daemonVersion {
			return versionMismatch(details, "CLI 与运行中 daemon 版本不一致")
		}
	}

	if cliVersion == "dev" {
		return Check{
			Name:        "version",
			Status:      StatusWarn,
			Summary:     "CLI 是未注入版本号的开发构建",
			Details:     details,
			Remediation: "使用 `make build` 或 `make install` 生成带版本号的 CLI。",
		}
	}
	if runtimeVersion == "" {
		return Check{
			Name:        "version",
			Status:      StatusWarn,
			Summary:     "Runtime 未声明版本，无法完成一致性检查",
			Details:     details,
			Remediation: "设置 AICODE_RUNTIME_VERSION，或使用版本化安装布局。",
		}
	}
	return Check{
		Name:    "version",
		Status:  StatusOK,
		Summary: "CLI 与 Runtime 版本一致",
		Details: details,
	}
}

func versionMismatch(details map[string]any, summary string) Check {
	return Check{
		Name:        "version",
		Status:      StatusError,
		Summary:     summary,
		Details:     details,
		Remediation: "重新运行 `make install`，然后执行 `aicode daemon stop && aicode daemon start`。",
	}
}

func checkPython(state runtimeState, deps dependencies) Check {
	if state.installErr != nil {
		return Check{
			Name:        "python",
			Status:      StatusError,
			Summary:     "Runtime Python 不可验证",
			Remediation: "先修复 Runtime 安装。",
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	output, err := deps.runCommand(
		ctx,
		state.installation.Python,
		"-c",
		"import platform; import fastapi, httpx, pydantic, uvicorn; print(platform.python_version())",
	)
	cancel()
	details := map[string]any{"executable": state.installation.Python}
	if output != "" {
		details["version"] = firstLine(output)
	}
	if err != nil {
		details["error"] = commandError(err, output)
		return Check{
			Name:        "python",
			Status:      StatusError,
			Summary:     "Runtime Python 或依赖不可用",
			Details:     details,
			Remediation: "重新运行 `make install` 以重建 venv 和锁定依赖。",
		}
	}
	pythonVersion := firstLine(output)
	if !pythonAtLeast(pythonVersion, 3, 11) {
		return Check{
			Name:        "python",
			Status:      StatusError,
			Summary:     "Runtime 需要 Python 3.11 或更高版本",
			Details:     details,
			Remediation: "安装 Python 3.11+，并通过 `make install INSTALL_PYTHON=python3.11` 重装。",
		}
	}
	return Check{
		Name:    "python",
		Status:  StatusOK,
		Summary: "Python 版本和 Runtime 依赖可用",
		Details: details,
	}
}

func checkPort(cfg config.Config, state runtimeState, deps dependencies) Check {
	details := map[string]any{
		"configured_port": cfg.Runtime.Port,
		"url":             cfg.Runtime.URL,
	}
	parsed, address, urlPort, err := runtimeAddress(cfg.Runtime.URL)
	if err != nil {
		details["error"] = err.Error()
		return Check{
			Name:        "port",
			Status:      StatusError,
			Summary:     "runtime.url 无效",
			Details:     details,
			Remediation: "使用 `aicode config set runtime.url http://127.0.0.1:8765` 修复 URL。",
		}
	}
	if cfg.Runtime.Port < 1 || cfg.Runtime.Port > 65535 {
		return Check{
			Name:        "port",
			Status:      StatusError,
			Summary:     "runtime.port 超出有效范围",
			Details:     details,
			Remediation: "将 runtime.port 设置为 1-65535 之间的端口。",
		}
	}
	if cfg.Runtime.Port != urlPort {
		details["url_port"] = urlPort
		return Check{
			Name:        "port",
			Status:      StatusError,
			Summary:     "runtime.url 与 runtime.port 不一致",
			Details:     details,
			Remediation: "将 runtime.url 中的端口与 runtime.port 设置为相同值。",
		}
	}

	if state.daemonErr == nil {
		daemonStatus := stringDetail(state.daemonStatus, "status")
		details["daemon_status"] = daemonStatus
		if pid, ok := state.daemonStatus["pid"]; ok {
			details["pid"] = pid
		}
		if daemonStatus != "ok" {
			return Check{
				Name:        "port",
				Status:      StatusError,
				Summary:     "daemon 响应异常",
				Details:     details,
				Remediation: "运行 `aicode daemon stop && aicode daemon start`，并检查 runtime.log。",
			}
		}
		return Check{
			Name:    "port",
			Status:  StatusOK,
			Summary: "Runtime daemon 正在监听且健康",
			Details: details,
		}
	}

	if !isLoopbackHost(parsed.Hostname()) {
		details["daemon_error"] = state.daemonErr.Error()
		return Check{
			Name:        "port",
			Status:      StatusWarn,
			Summary:     "远程 Runtime 不可达，未检查本地端口",
			Details:     details,
			Remediation: "确认远程 Runtime 地址、网络和 daemon 状态。",
		}
	}

	open, dialErr := deps.portOpen(address, 400*time.Millisecond)
	if open {
		details["address"] = address
		details["daemon_error"] = state.daemonErr.Error()
		return Check{
			Name:        "port",
			Status:      StatusError,
			Summary:     "端口已被占用，但目标不是健康的 aicode Runtime",
			Details:     details,
			Remediation: "释放该端口，或同时修改 runtime.url 与 runtime.port。",
		}
	}
	details["address"] = address
	if dialErr != nil && !errors.Is(dialErr, syscall.ECONNREFUSED) {
		details["probe_error"] = dialErr.Error()
		return Check{
			Name:        "port",
			Status:      StatusWarn,
			Summary:     "Runtime daemon 未运行，无法确认端口是否可用",
			Details:     details,
			Remediation: "检查本机网络权限和端口配置，然后运行 `aicode daemon start`。",
		}
	}
	return Check{
		Name:        "port",
		Status:      StatusWarn,
		Summary:     "Runtime daemon 未运行，配置端口当前可用",
		Details:     details,
		Remediation: "运行 `aicode daemon start`；普通 agent 命令也会自动启动 daemon。",
	}
}

func checkProvider(cfg config.Config, deps dependencies) Check {
	providerType := strings.TrimSpace(cfg.Provider.Type)
	details := map[string]any{"type": providerType}
	var baseURL string
	var apiKeyEnv string
	var directEnv string
	switch providerType {
	case "openai_compatible":
		baseURL = cfg.OpenAICompatible.BaseURL
		apiKeyEnv = cfg.OpenAICompatible.APIKeyEnv
		directEnv = "AICODE_OPENAI_API_KEY"
	case "anthropic":
		baseURL = cfg.Anthropic.BaseURL
		apiKeyEnv = cfg.Anthropic.APIKeyEnv
	default:
		return Check{
			Name:        "provider",
			Status:      StatusError,
			Summary:     "provider.type 不受支持",
			Details:     details,
			Remediation: "将 provider.type 设置为 openai_compatible 或 anthropic。",
		}
	}

	baseURL = strings.TrimSpace(baseURL)
	apiKeyEnv = strings.TrimSpace(apiKeyEnv)
	details["base_url"] = baseURL
	details["api_key_env"] = apiKeyEnv
	if err := validateProviderURL(baseURL); err != nil {
		details["error"] = err.Error()
		return Check{
			Name:        "provider",
			Status:      StatusError,
			Summary:     "provider base URL 无效",
			Details:     details,
			Remediation: "通过 `aicode config set` 配置有效的 http/https provider base URL。",
		}
	}
	if apiKeyEnv == "" {
		return Check{
			Name:        "provider",
			Status:      StatusError,
			Summary:     "provider 的 API key 环境变量名为空",
			Details:     details,
			Remediation: "设置对应 provider 的 api_key_env 配置。",
		}
	}

	keySource := ""
	if directEnv != "" && nonEmptyEnv(deps.lookupEnv, directEnv) {
		keySource = directEnv
	} else if nonEmptyEnv(deps.lookupEnv, apiKeyEnv) {
		keySource = apiKeyEnv
	}
	details["configured"] = keySource != ""
	if keySource != "" {
		details["api_key_source"] = keySource
		return Check{
			Name:    "provider",
			Status:  StatusOK,
			Summary: "provider 配置和 API key 已就绪",
			Details: details,
		}
	}
	return Check{
		Name:        "provider",
		Status:      StatusWarn,
		Summary:     "provider API key 尚未配置",
		Details:     details,
		Remediation: fmt.Sprintf("导出 %s 后重试；doctor 不会发送外部请求。", apiKeyEnv),
	}
}

func checkDocker(deps dependencies) Check {
	path, err := deps.lookPath("docker")
	if err != nil {
		return Check{
			Name:        "docker",
			Status:      StatusWarn,
			Summary:     "未找到 Docker CLI（仅 sandbox 功能需要）",
			Remediation: "如需 `--sandbox docker`，请安装并启动 Docker。",
		}
	}
	details := map[string]any{"executable": path}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	output, commandErr := deps.runCommand(ctx, path, "version", "--format", "{{.Server.Version}}")
	cancel()
	if commandErr != nil {
		details["error"] = commandError(commandErr, output)
		return Check{
			Name:        "docker",
			Status:      StatusWarn,
			Summary:     "Docker CLI 已安装，但 daemon 不可用",
			Details:     details,
			Remediation: "启动 Docker daemon；不使用 sandbox 时可忽略此项。",
		}
	}
	details["server_version"] = firstLine(output)
	return Check{
		Name:    "docker",
		Status:  StatusOK,
		Summary: "Docker daemon 可用",
		Details: details,
	}
}

func runtimeAddress(rawURL string) (*url.URL, string, int, error) {
	parsed, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return nil, "", 0, err
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return nil, "", 0, fmt.Errorf("scheme 必须是 http 或 https")
	}
	host := parsed.Hostname()
	if host == "" {
		return nil, "", 0, fmt.Errorf("缺少 host")
	}
	port := 80
	if parsed.Scheme == "https" {
		port = 443
	}
	if rawPort := parsed.Port(); rawPort != "" {
		port, err = strconv.Atoi(rawPort)
		if err != nil || port < 1 || port > 65535 {
			return nil, "", 0, fmt.Errorf("端口无效")
		}
	}
	return parsed, net.JoinHostPort(host, strconv.Itoa(port)), port, nil
}

func validateProviderURL(rawURL string) error {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return err
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return fmt.Errorf("scheme 必须是 http 或 https")
	}
	if parsed.Hostname() == "" {
		return fmt.Errorf("缺少 host")
	}
	return nil
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func pythonAtLeast(value string, major int, minor int) bool {
	parts := strings.Split(strings.TrimSpace(value), ".")
	if len(parts) < 2 {
		return false
	}
	gotMajor, majorErr := strconv.Atoi(parts[0])
	gotMinor, minorErr := strconv.Atoi(parts[1])
	if majorErr != nil || minorErr != nil {
		return false
	}
	return gotMajor > major || (gotMajor == major && gotMinor >= minor)
}

func aggregateStatus(checks []Check) string {
	status := StatusOK
	for _, check := range checks {
		if check.Status == StatusError {
			return StatusError
		}
		if check.Status == StatusWarn {
			status = StatusWarn
		}
	}
	return status
}

func normalizedVersion(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "dev"
	}
	return value
}

func stringDetail(value map[string]any, key string) string {
	if value == nil {
		return ""
	}
	text, _ := value[key].(string)
	return strings.TrimSpace(text)
}

func nonEmptyEnv(lookup func(string) (string, bool), key string) bool {
	value, ok := lookup(key)
	return ok && strings.TrimSpace(value) != ""
}

func firstLine(value string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(value), "\n")
	return strings.TrimSpace(line)
}

func commandError(err error, output string) string {
	if output == "" {
		return err.Error()
	}
	return fmt.Sprintf("%v: %s", err, firstLine(output))
}

func renderJSON(writer io.Writer, report Report) error {
	encoder := json.NewEncoder(writer)
	encoder.SetIndent("", "  ")
	encoder.SetEscapeHTML(false)
	return encoder.Encode(report)
}

func renderHuman(writer io.Writer, report Report) error {
	if _, err := fmt.Fprintf(writer, "aicode doctor\n状态: %s\nCLI: %s\n\n", report.Status, report.CLIVersion); err != nil {
		return err
	}
	for _, check := range report.Checks {
		if _, err := fmt.Fprintf(writer, "[%s] %s: %s\n", strings.ToUpper(check.Status), check.Name, check.Summary); err != nil {
			return err
		}
		keys := make([]string, 0, len(check.Details))
		for key := range check.Details {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			if _, err := fmt.Fprintf(writer, "  %s: %v\n", key, check.Details[key]); err != nil {
				return err
			}
		}
		if check.Remediation != "" {
			if _, err := fmt.Fprintf(writer, "  修复: %s\n", check.Remediation); err != nil {
				return err
			}
		}
	}
	return nil
}
