package main

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/aicode-dev/aicode/cli/internal/client"
	"github.com/aicode-dev/aicode/cli/internal/config"
	"github.com/aicode-dev/aicode/cli/internal/daemon"
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
		return runConfigCommand(args[1:])
	case "sessions":
		return runSimpleGet(cfg, "/v1/sessions")
	case "usage":
		return runSimpleGet(cfg, "/v1/usage")
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
  aicode explain src/foo.ts
  aicode diff
  aicode test
  aicode sessions
  aicode resume --last
  aicode usage
  aicode config init
  aicode config show
  aicode config set ui.language en-US
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

func runConfigCommand(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("用法: aicode config <init|show|set>")
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
	default:
		return fmt.Errorf("未知 config 命令: %s", args[0])
	}
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

func runSimpleGet(cfg config.Config, path string) error {
	if err := ensureDaemon(cfg); err != nil {
		return err
	}

	ctx, cancel := context.WithTimeout(context.Background(), defaultTimeout)
	defer cancel()

	api := client.New(cfg.Runtime.URL)
	value, err := api.GetJSON(ctx, path)
	if err != nil {
		return err
	}
	renderer.PrintJSON(value)
	return nil
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
