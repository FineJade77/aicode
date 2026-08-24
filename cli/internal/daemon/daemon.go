package daemon

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/FineJade77/aicode/cli/internal/config"
)

const installManifestSchemaVersion = 1

type RuntimeInstallation struct {
	RuntimeDir string
	Python     string
	Version    string
	Source     string
}

type installManifest struct {
	SchemaVersion int    `json:"schema_version"`
	Version       string `json:"version"`
	RuntimeDir    string `json:"runtime_dir"`
	Python        string `json:"python"`
}

func tokenPath(home string) string {
	return filepath.Join(home, "runtime.token")
}

func generateToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

// Token returns the persisted Runtime authentication token. A missing or
// unreadable token is treated as unconfigured; Runtime enforces rejection.
func Token() string {
	home, err := config.Home()
	if err != nil {
		return ""
	}
	content, err := os.ReadFile(tokenPath(home))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(content))
}

func Status(ctx context.Context, baseURL string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+"/v1/daemon/status", nil)
	if err != nil {
		return nil, err
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("daemon status failed: %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}

	var status map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&status); err != nil {
		return nil, err
	}
	return status, nil
}

func Start(cfg config.Config) error {
	installation, err := ResolveRuntime()
	if err != nil {
		return err
	}

	home, err := config.Home()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(home, 0o755); err != nil {
		return err
	}

	token, err := generateToken()
	if err != nil {
		return fmt.Errorf("failed to generate authentication token: %w", err)
	}
	if err := os.WriteFile(tokenPath(home), []byte(token), 0o600); err != nil {
		return fmt.Errorf("failed to write authentication token: %w", err)
	}

	logFile, err := os.OpenFile(filepath.Join(home, "runtime.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer logFile.Close()

	cmd := exec.Command(
		installation.Python,
		"-m",
		"uvicorn",
		"app.server.main:app",
		"--host",
		"127.0.0.1",
		"--port",
		strconv.Itoa(cfg.Runtime.Port),
	)
	cmd.Dir = installation.RuntimeDir
	cmd.Env = append(cfg.RuntimeEnv(), "AICODE_RUNTIME_TOKEN="+token)
	if installation.Version != "" {
		cmd.Env = append(cmd.Env, "AICODE_RUNTIME_VERSION="+installation.Version)
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("failed to start Runtime: %w", err)
	}

	pidPath := filepath.Join(home, "runtime.pid")
	if err := os.WriteFile(pidPath, []byte(strconv.Itoa(cmd.Process.Pid)), 0o600); err != nil {
		return err
	}
	return cmd.Process.Release()
}

func Stop() error {
	home, err := config.Home()
	if err != nil {
		return err
	}
	pidPath := filepath.Join(home, "runtime.pid")
	content, err := os.ReadFile(pidPath)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}

	pid, err := strconv.Atoi(strings.TrimSpace(string(content)))
	if err != nil {
		return err
	}
	prepareRuntimeStop()
	proc, err := os.FindProcess(pid)
	if err != nil {
		return err
	}
	if err := proc.Kill(); err != nil {
		if !strings.Contains(err.Error(), "process already finished") {
			return err
		}
	}
	if err := os.Remove(pidPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.Remove(tokenPath(home)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func prepareRuntimeStop() {
	cfg, err := config.Load()
	if err != nil || !loopbackRuntimeURL(cfg.Runtime.URL) {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		strings.TrimRight(cfg.Runtime.URL, "/")+"/v1/daemon/prepare-stop",
		nil,
	)
	if err != nil {
		return
	}
	if token := Token(); token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err == nil {
		_ = resp.Body.Close()
	}
}

func loopbackRuntimeURL(rawURL string) bool {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return false
	}
	host := parsed.Hostname()
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func WaitUntilReady(baseURL string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
		_, err := Status(ctx, baseURL)
		cancel()
		if err == nil {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return fmt.Errorf("Runtime daemon startup timed out; inspect the log")
}

func RuntimeDir() (string, error) {
	installation, err := ResolveRuntime()
	if err != nil {
		return "", err
	}
	return installation.RuntimeDir, nil
}

func ResolveRuntime() (RuntimeInstallation, error) {
	if explicit := os.Getenv("AICODE_RUNTIME_DIR"); explicit != "" {
		runtimeDir, err := filepath.Abs(explicit)
		if err != nil {
			return RuntimeInstallation{}, err
		}
		if err := validateRuntimeDir(runtimeDir); err != nil {
			return RuntimeInstallation{}, err
		}
		return RuntimeInstallation{
			RuntimeDir: runtimeDir,
			Python:     runtimePythonOverride(),
			Version:    strings.TrimSpace(os.Getenv("AICODE_RUNTIME_VERSION")),
			Source:     "environment",
		}, nil
	}

	if explicitManifest := os.Getenv("AICODE_INSTALL_MANIFEST"); explicitManifest != "" {
		return runtimeInstallationFromManifest(explicitManifest)
	}

	if executable, err := os.Executable(); err == nil {
		manifestPath := manifestPathForExecutable(executable)
		if _, statErr := os.Stat(manifestPath); statErr == nil {
			return runtimeInstallationFromManifest(manifestPath)
		} else if !errors.Is(statErr, os.ErrNotExist) {
			return RuntimeInstallation{}, fmt.Errorf("failed to inspect installation manifest: %w", statErr)
		}
	}

	cwd, err := os.Getwd()
	if err != nil {
		return RuntimeInstallation{}, err
	}
	runtimeDir, err := sourceRuntimeFrom(cwd)
	if err != nil {
		return RuntimeInstallation{}, err
	}
	return RuntimeInstallation{
		RuntimeDir: runtimeDir,
		Python:     runtimePythonOverride(),
		Version:    sourceRuntimeVersion(runtimeDir),
		Source:     "source-checkout",
	}, nil
}

func sourceRuntimeVersion(runtimeDir string) string {
	if explicit := strings.TrimSpace(os.Getenv("AICODE_RUNTIME_VERSION")); explicit != "" {
		return explicit
	}
	content, err := os.ReadFile(filepath.Join(filepath.Dir(runtimeDir), "VERSION"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(content))
}

func sourceRuntimeFrom(start string) (string, error) {
	cwd, err := filepath.Abs(start)
	if err != nil {
		return "", err
	}
	for {
		candidate := filepath.Join(cwd, "runtime")
		if err := validateRuntimeDir(candidate); err == nil {
			return candidate, nil
		}

		parent := filepath.Dir(cwd)
		if parent == cwd {
			break
		}
		cwd = parent
	}

	return "", fmt.Errorf("runtime/app/server/main.py was not found; set AICODE_RUNTIME_DIR if needed")
}

func runtimeInstallationFromManifest(path string) (RuntimeInstallation, error) {
	manifestPath, err := filepath.Abs(path)
	if err != nil {
		return RuntimeInstallation{}, err
	}
	content, err := os.ReadFile(manifestPath)
	if err != nil {
		return RuntimeInstallation{}, fmt.Errorf("failed to read installation manifest: %w", err)
	}
	var manifest installManifest
	if err := json.Unmarshal(content, &manifest); err != nil {
		return RuntimeInstallation{}, fmt.Errorf("failed to parse installation manifest: %w", err)
	}
	if manifest.SchemaVersion != installManifestSchemaVersion {
		return RuntimeInstallation{}, fmt.Errorf(
			"unsupported installation manifest schema_version %d; supported version is %d",
			manifest.SchemaVersion,
			installManifestSchemaVersion,
		)
	}
	if strings.TrimSpace(manifest.Version) == "" {
		return RuntimeInstallation{}, fmt.Errorf("installation manifest is missing version")
	}

	root := filepath.Dir(manifestPath)
	runtimeDir, err := resolveManifestPath(root, manifest.RuntimeDir, "runtime_dir")
	if err != nil {
		return RuntimeInstallation{}, err
	}
	python, err := resolveManifestPath(root, manifest.Python, "python")
	if err != nil {
		return RuntimeInstallation{}, err
	}
	if err := validateRuntimeDir(runtimeDir); err != nil {
		return RuntimeInstallation{}, err
	}
	info, err := os.Stat(python)
	if err != nil {
		return RuntimeInstallation{}, fmt.Errorf("Python from the installation manifest is unavailable: %w", err)
	}
	if info.IsDir() {
		return RuntimeInstallation{}, fmt.Errorf("python in the installation manifest points to a directory: %s", python)
	}
	return RuntimeInstallation{
		RuntimeDir: runtimeDir,
		Python:     python,
		Version:    manifest.Version,
		Source:     "install-manifest",
	}, nil
}

func manifestPathForExecutable(executable string) string {
	resolved := executable
	if evaluated, err := filepath.EvalSymlinks(executable); err == nil {
		resolved = evaluated
	}
	prefix := filepath.Dir(filepath.Dir(resolved))
	return filepath.Join(prefix, "lib", "aicode", "manifest.json")
}

func resolveManifestPath(root string, value string, field string) (string, error) {
	if strings.TrimSpace(value) == "" {
		return "", fmt.Errorf("installation manifest is missing %s", field)
	}
	if filepath.IsAbs(value) {
		return "", fmt.Errorf("%s in the installation manifest must be a relative path", field)
	}
	resolved := filepath.Join(root, filepath.Clean(value))
	relative, err := filepath.Rel(root, resolved)
	if err != nil {
		return "", fmt.Errorf("failed to resolve %s from the installation manifest: %w", field, err)
	}
	if relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("%s in the installation manifest must not escape the installation directory", field)
	}
	return resolved, nil
}

func validateRuntimeDir(runtimeDir string) error {
	entrypoint := filepath.Join(runtimeDir, "app", "server", "main.py")
	info, err := os.Stat(entrypoint)
	if err != nil {
		return fmt.Errorf("Runtime entrypoint is unavailable at %s: %w", entrypoint, err)
	}
	if info.IsDir() {
		return fmt.Errorf("Runtime entrypoint is not a file: %s", entrypoint)
	}
	return nil
}

func runtimePythonOverride() string {
	if explicit := strings.TrimSpace(os.Getenv("AICODE_RUNTIME_PYTHON")); explicit != "" {
		return explicit
	}
	return "python3"
}
