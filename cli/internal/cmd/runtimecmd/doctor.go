package runtimecmd

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

var ErrChecksFailed = errors.New("doctor found errors; follow the remediation guidance")

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

func runDoctor(cfg config.Config, args []string) error {
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
			return false, fmt.Errorf("usage: aicode runtime doctor [--json]")
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
		checkConfigDeprecations(cfg),
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
			Summary:     "Could not resolve the Runtime installation",
			Details:     map[string]any{"error": state.installErr.Error()},
			Remediation: "Run `make install`, or set a valid AICODE_RUNTIME_DIR for source development.",
		}
	}
	return Check{
		Name:    "installation",
		Status:  StatusOK,
		Summary: "Runtime installation layout is valid",
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
			Summary:     "Could not verify CLI and Runtime versions",
			Details:     details,
			Remediation: "Repair the Runtime installation, then run `aicode runtime doctor` again.",
		}
	}

	runtimeVersion := strings.TrimSpace(state.installation.Version)
	details["runtime"] = runtimeVersion
	daemonVersion := stringDetail(state.daemonStatus, "version")
	if daemonVersion != "" {
		details["daemon"] = daemonVersion
	}

	if runtimeVersion != "" && daemonVersion != "" && runtimeVersion != daemonVersion {
		return versionMismatch(details, "Installed Runtime and running daemon versions do not match")
	}
	if cliVersion != "dev" {
		if runtimeVersion != "" && cliVersion != runtimeVersion {
			return versionMismatch(details, "CLI and installed Runtime versions do not match")
		}
		if daemonVersion != "" && cliVersion != daemonVersion {
			return versionMismatch(details, "CLI and running daemon versions do not match")
		}
	}

	if cliVersion == "dev" {
		return Check{
			Name:        "version",
			Status:      StatusWarn,
			Summary:     "CLI is a development build without an injected version",
			Details:     details,
			Remediation: "Use `make build` or `make install` to produce a versioned CLI.",
		}
	}
	if runtimeVersion == "" {
		return Check{
			Name:        "version",
			Status:      StatusWarn,
			Summary:     "Runtime does not declare a version, so consistency cannot be checked",
			Details:     details,
			Remediation: "Set AICODE_RUNTIME_VERSION or use the versioned installation layout.",
		}
	}
	return Check{
		Name:    "version",
		Status:  StatusOK,
		Summary: "CLI and Runtime versions match",
		Details: details,
	}
}

func versionMismatch(details map[string]any, summary string) Check {
	return Check{
		Name:        "version",
		Status:      StatusError,
		Summary:     summary,
		Details:     details,
		Remediation: "Run `make install` again, then run `aicode runtime stop && aicode runtime start`.",
	}
}

func checkPython(state runtimeState, deps dependencies) Check {
	if state.installErr != nil {
		return Check{
			Name:        "python",
			Status:      StatusError,
			Summary:     "Runtime Python could not be verified",
			Remediation: "Repair the Runtime installation first.",
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
			Summary:     "Runtime Python or its dependencies are unavailable",
			Details:     details,
			Remediation: "Run `make install` again to rebuild the virtual environment and locked dependencies.",
		}
	}
	pythonVersion := firstLine(output)
	if !pythonAtLeast(pythonVersion, 3, 11) {
		return Check{
			Name:        "python",
			Status:      StatusError,
			Summary:     "Runtime requires Python 3.11 or later",
			Details:     details,
			Remediation: "Install Python 3.11+ and reinstall with `make install INSTALL_PYTHON=python3.11`.",
		}
	}
	return Check{
		Name:    "python",
		Status:  StatusOK,
		Summary: "Python version and Runtime dependencies are available",
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
			Summary:     "runtime.url is invalid",
			Details:     details,
			Remediation: "Set a valid URL with `aicode config set runtime.url http://127.0.0.1:8765`.",
		}
	}
	if cfg.Runtime.Port < 1 || cfg.Runtime.Port > 65535 {
		return Check{
			Name:        "port",
			Status:      StatusError,
			Summary:     "runtime.port is outside the valid range",
			Details:     details,
			Remediation: "Set runtime.port to a value between 1 and 65535.",
		}
	}
	if cfg.Runtime.Port != urlPort {
		details["url_port"] = urlPort
		return Check{
			Name:        "port",
			Status:      StatusError,
			Summary:     "runtime.url and runtime.port do not match",
			Details:     details,
			Remediation: "Set the port in runtime.url and runtime.port to the same value.",
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
				Summary:     "Daemon response is invalid",
				Details:     details,
				Remediation: "Run `aicode runtime stop && aicode runtime start`, then inspect runtime.log.",
			}
		}
		return Check{
			Name:    "port",
			Status:  StatusOK,
			Summary: "Runtime daemon is listening and healthy",
			Details: details,
		}
	}

	if !isLoopbackHost(parsed.Hostname()) {
		details["daemon_error"] = state.daemonErr.Error()
		return Check{
			Name:        "port",
			Status:      StatusWarn,
			Summary:     "Remote Runtime is unreachable; local port was not checked",
			Details:     details,
			Remediation: "Verify the remote Runtime address, network, and daemon status.",
		}
	}

	open, dialErr := deps.portOpen(address, 400*time.Millisecond)
	if open {
		details["address"] = address
		details["daemon_error"] = state.daemonErr.Error()
		return Check{
			Name:        "port",
			Status:      StatusError,
			Summary:     "Port is occupied, but the target is not a healthy aicode Runtime",
			Details:     details,
			Remediation: "Release the port or update both runtime.url and runtime.port.",
		}
	}
	details["address"] = address
	if dialErr != nil && !errors.Is(dialErr, syscall.ECONNREFUSED) {
		details["probe_error"] = dialErr.Error()
		return Check{
			Name:        "port",
			Status:      StatusWarn,
			Summary:     "Runtime daemon is not running and port availability could not be determined",
			Details:     details,
			Remediation: "Check local network permissions and port configuration, then run `aicode runtime start`.",
		}
	}
	return Check{
		Name:        "port",
		Status:      StatusWarn,
		Summary:     "Runtime daemon is not running and the configured port is available",
		Details:     details,
		Remediation: "Run `aicode runtime start`; normal agent commands also start the daemon automatically.",
	}
}

func checkProvider(cfg config.Config, deps dependencies) Check {
	providerType := strings.TrimSpace(cfg.Provider.Type)
	details := map[string]any{"type": providerType}
	var baseURL string
	var apiKeyEnv string
	var directEnv string
	authMode := "required"
	switch providerType {
	case "openai_compatible":
		baseURL = cfg.OpenAICompatible.BaseURL
		apiKeyEnv = cfg.OpenAICompatible.APIKeyEnv
		directEnv = "AICODE_OPENAI_API_KEY"
		authMode = strings.TrimSpace(cfg.OpenAICompatible.AuthMode)
		details["profile"] = cfg.OpenAICompatible.Profile
		details["profile_schema_version"] = cfg.OpenAICompatible.ProfileSchemaVersion
		details["auth_mode"] = authMode
		details["context_window"] = cfg.OpenAICompatible.ContextWindow
		details["max_output_tokens"] = cfg.OpenAICompatible.MaxOutputTokens
		details["tool_calling"] = cfg.OpenAICompatible.ToolCalling
		details["streaming"] = cfg.OpenAICompatible.Streaming
		details["tokenizer"] = cfg.OpenAICompatible.Tokenizer
		if authMode != "required" && authMode != "optional" && authMode != "none" {
			return Check{
				Name:        "provider",
				Status:      StatusError,
				Summary:     "Provider auth_mode is unsupported",
				Details:     details,
				Remediation: "Set provider.openai_compatible.auth_mode to required, optional, or none.",
			}
		}
		if cfg.OpenAICompatible.ProfileSchemaVersion != 1 ||
			strings.TrimSpace(cfg.OpenAICompatible.Profile) == "" ||
			cfg.OpenAICompatible.ContextWindow <= 0 ||
			cfg.OpenAICompatible.MaxOutputTokens <= 0 ||
			cfg.OpenAICompatible.MaxOutputTokens >= cfg.OpenAICompatible.ContextWindow ||
			cfg.OpenAICompatible.Tokenizer != "chars" ||
			cfg.OpenAICompatible.CharsPerToken < 1 ||
			cfg.OpenAICompatible.CharsPerToken > 20 ||
			cfg.OpenAICompatible.TimeoutSeconds <= 0 {
			return Check{
				Name:        "provider",
				Status:      StatusError,
				Summary:     "Provider Profile capability configuration is invalid",
				Details:     details,
				Remediation: "Use schema version 1, positive context and max output values with max output below context, and the chars tokenizer.",
			}
		}
	case "anthropic":
		baseURL = cfg.Anthropic.BaseURL
		apiKeyEnv = cfg.Anthropic.APIKeyEnv
	default:
		return Check{
			Name:        "provider",
			Status:      StatusError,
			Summary:     "provider.type is unsupported",
			Details:     details,
			Remediation: "Set provider.type to openai_compatible or anthropic.",
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
			Summary:     "Provider base URL is invalid",
			Details:     details,
			Remediation: "Use `aicode config set` to configure a valid HTTP or HTTPS provider base URL.",
		}
	}
	if authMode == "required" && apiKeyEnv == "" {
		return Check{
			Name:        "provider",
			Status:      StatusError,
			Summary:     "Provider API key environment-variable name is empty",
			Details:     details,
			Remediation: "Set api_key_env for the selected provider.",
		}
	}

	keySource := ""
	if directEnv != "" && nonEmptyEnv(deps.lookupEnv, directEnv) {
		keySource = directEnv
	} else if nonEmptyEnv(deps.lookupEnv, apiKeyEnv) {
		keySource = apiKeyEnv
	}
	configured := authMode != "required" || keySource != ""
	details["configured"] = configured
	if authMode == "none" {
		return Check{
			Name:    "provider",
			Status:  StatusOK,
			Summary: "No-auth local Provider Profile is ready",
			Details: details,
		}
	}
	if keySource != "" {
		details["api_key_source"] = keySource
		return Check{
			Name:    "provider",
			Status:  StatusOK,
			Summary: "Provider configuration and API key are ready",
			Details: details,
		}
	}
	if authMode == "optional" {
		return Check{
			Name:    "provider",
			Status:  StatusOK,
			Summary: "Optional-auth Provider Profile is ready (no API key will be sent)",
			Details: details,
		}
	}
	return Check{
		Name:        "provider",
		Status:      StatusWarn,
		Summary:     "Provider API key is not configured",
		Details:     details,
		Remediation: fmt.Sprintf("Export %s and retry; doctor does not send external requests.", apiKeyEnv),
	}
}

func checkDocker(deps dependencies) Check {
	path, err := deps.lookPath("docker")
	if err != nil {
		return Check{
			Name:        "docker",
			Status:      StatusWarn,
			Summary:     "Docker CLI was not found (required only for sandbox features)",
			Remediation: "Install and start Docker to use `aicode project sandbox`.",
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
			Summary:     "Docker CLI is installed, but the daemon is unavailable",
			Details:     details,
			Remediation: "Start the Docker daemon; ignore this check if you do not use the sandbox.",
		}
	}
	details["server_version"] = firstLine(output)

	// aicode never pulls sandbox images implicitly, so a missing image only
	// surfaces mid-task as a failed tool call. Report it at setup time instead.
	missing := missingSandboxImages(deps, path)
	if len(missing) > 0 {
		details["missing_images"] = missing
		return Check{
			Name:    "docker",
			Status:  StatusWarn,
			Summary: "Docker daemon is available, but sandbox images are missing",
			Details: details,
			Remediation: fmt.Sprintf(
				"Run `docker pull %s`; aicode does not pull images implicitly, so sandboxed commands fail until it is present.",
				strings.Join(missing, "` and `docker pull "),
			),
		}
	}
	return Check{
		Name:    "docker",
		Status:  StatusOK,
		Summary: "Docker daemon is available",
		Details: details,
	}
}

// sandboxImages mirrors docker_image() in runtime/app/execution/docker.py. Only
// the default image is required; language-specific images are pulled on demand
// by the user when they first sandbox that toolchain.
var sandboxImages = []string{"ubuntu:24.04"}

func missingSandboxImages(deps dependencies, dockerPath string) []string {
	var missing []string
	for _, image := range sandboxImages {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		_, err := deps.runCommand(ctx, dockerPath, "image", "inspect", image)
		cancel()
		if err != nil {
			missing = append(missing, image)
		}
	}
	return missing
}

func runtimeAddress(rawURL string) (*url.URL, string, int, error) {
	parsed, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return nil, "", 0, err
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return nil, "", 0, fmt.Errorf("scheme must be http or https")
	}
	host := parsed.Hostname()
	if host == "" {
		return nil, "", 0, fmt.Errorf("host is missing")
	}
	port := 80
	if parsed.Scheme == "https" {
		port = 443
	}
	if rawPort := parsed.Port(); rawPort != "" {
		port, err = strconv.Atoi(rawPort)
		if err != nil || port < 1 || port > 65535 {
			return nil, "", 0, fmt.Errorf("invalid port")
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
		return fmt.Errorf("scheme must be http or https")
	}
	if parsed.Hostname() == "" {
		return fmt.Errorf("host is missing")
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
	if _, err := fmt.Fprintf(writer, "aicode runtime doctor\nStatus: %s\nCLI: %s\n\n", report.Status, report.CLIVersion); err != nil {
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
			if _, err := fmt.Fprintf(writer, "  Fix: %s\n", check.Remediation); err != nil {
				return err
			}
		}
	}
	return nil
}

// checkConfigDeprecations reports legacy configuration keys and what became of
// them.
//
// A warning rather than an error: the settings still take effect, so the
// installation works. What it prevents is the quieter failure — a legacy key
// that lost to its replacement looks exactly like one that applied, and a user
// deleting it cannot tell whether the model they are running is about to
// change.
func checkConfigDeprecations(cfg config.Config) Check {
	if len(cfg.Deprecations) == 0 {
		return Check{
			Name:    "config",
			Status:  StatusOK,
			Summary: "configuration uses current setting names",
		}
	}
	details := map[string]any{}
	keys := make([]string, 0, len(cfg.Deprecations))
	for _, deprecation := range cfg.Deprecations {
		details[deprecation.Key] = deprecation.Effect
		keys = append(keys, deprecation.Key)
	}
	return Check{
		Name:        "config",
		Status:      StatusWarn,
		Summary:     fmt.Sprintf("%d deprecated setting(s): %s", len(keys), strings.Join(keys, ", ")),
		Details:     details,
		Remediation: "Rename these to models.main (or delete them) in `aicode config path`; a future release stops reading them.",
	}
}
