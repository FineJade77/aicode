package daemon

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/FineJade77/aicode/cli/internal/config"
)

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

// Token 返回当前持久化的 Runtime 认证 token；找不到或读取失败时返回空字符串，
// 与"未配置认证"视为同一种情况——真正的拒绝逻辑在 Runtime 侧强制执行。
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
	runtimeDir, err := RuntimeDir()
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
		return fmt.Errorf("生成认证 token 失败: %w", err)
	}
	if err := os.WriteFile(tokenPath(home), []byte(token), 0o600); err != nil {
		return fmt.Errorf("写入认证 token 失败: %w", err)
	}

	logFile, err := os.OpenFile(filepath.Join(home, "runtime.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	defer logFile.Close()

	cmd := exec.Command(
		"python3",
		"-m",
		"uvicorn",
		"app.server.main:app",
		"--host",
		"127.0.0.1",
		"--port",
		strconv.Itoa(cfg.Runtime.Port),
	)
	cmd.Dir = runtimeDir
	cmd.Env = append(cfg.RuntimeEnv(), "AICODE_RUNTIME_TOKEN="+token)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("启动 Runtime 失败: %w", err)
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
	return fmt.Errorf("Runtime daemon 启动超时，请查看日志")
}

func RuntimeDir() (string, error) {
	if explicit := os.Getenv("AICODE_RUNTIME_DIR"); explicit != "" {
		return explicit, nil
	}

	cwd, err := os.Getwd()
	if err != nil {
		return "", err
	}

	for {
		candidate := filepath.Join(cwd, "runtime")
		if _, err := os.Stat(filepath.Join(candidate, "app", "server", "main.py")); err == nil {
			return candidate, nil
		}

		parent := filepath.Dir(cwd)
		if parent == cwd {
			break
		}
		cwd = parent
	}

	return "", fmt.Errorf("未找到 runtime/app/server/main.py，可设置 AICODE_RUNTIME_DIR")
}
