package main

import (
	"fmt"
	"os"
	"strings"

	"github.com/FineJade77/aicode/cli/internal/cmd/agentrun"
	"github.com/FineJade77/aicode/cli/internal/cmd/cancelcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/commitmsgcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/configcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/daemoncmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/doctorcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/modelscmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/replcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/resumecmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/runtimeio"
	"github.com/FineJade77/aicode/cli/internal/cmd/sandboxcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/trustcmd"
	"github.com/FineJade77/aicode/cli/internal/cmd/usagecmd"
	"github.com/FineJade77/aicode/cli/internal/config"
)

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
		return sandboxcmd.Run(cfg, options.Sandbox, commandArgs)
	}
	args = commandArgs

	if len(args) == 0 {
		printHelp()
		return nil
	}

	switch args[0] {
	case "daemon":
		return daemoncmd.Run(cfg, args[1:])
	case "doctor":
		return doctorcmd.Run(cfg, args[1:])
	case "trust":
		return trustcmd.Run(cfg, args[1:])
	case "config":
		return configcmd.Run(cfg, args[1:])
	case "sessions":
		return runtimeio.RunSimpleGet(cfg, "/v1/sessions")
	case "usage":
		return usagecmd.Run(cfg, args[1:])
	case "models":
		return modelscmd.Run(cfg, args[1:])
	case "review-rules":
		return configcmd.ReviewRules(cfg)
	case "resume":
		return resumecmd.Run(cfg, args[1:])
	case "cancel":
		return cancelcmd.Run(cfg, args[1:])
	case "review":
		return agentrun.Run(cfg, "review", "请审查当前代码变更。")
	case "diff":
		return agentrun.Run(cfg, "diff", "请查看当前 git diff 并总结变更。")
	case "test":
		return agentrun.Run(cfg, "test", "请自动发现并运行当前项目的低风险测试命令。")
	case "commit-message":
		return commitmsgcmd.Run(cfg)
	case "explain":
		if len(args) < 2 {
			return fmt.Errorf("用法: aicode explain <file-or-symbol>")
		}
		return agentrun.Run(cfg, "explain", "请解释 "+strings.Join(args[1:], " "))
	case "chat":
		if len(args) < 2 {
			return replcmd.Run(cfg)
		}
		return agentrun.Run(cfg, "chat", strings.Join(args[1:], " "))
	case "repl":
		return replcmd.Run(cfg)
	default:
		return agentrun.Run(cfg, "default", strings.Join(args, " "))
	}
}

func printHelp() {
	fmt.Println(`aicode - 本地优先的 CLI Coding Agent

用法:
  aicode "修复这个测试失败"
  aicode chat
  aicode repl
  aicode chat "解释当前目录"
  aicode review
  aicode review-rules
  aicode explain src/foo.ts
  aicode diff
  aicode test
  aicode commit-message
  aicode --sandbox docker test
  aicode --sandbox docker build
  aicode --sandbox docker lint
  aicode sessions
  aicode resume --last
  aicode resume --last "继续刚才的任务"
  aicode resume <session_id> "继续这个会话"
  aicode cancel --last
  aicode cancel <session_id>
  aicode usage [--json]
  aicode usage --today [--json]
  aicode usage --session <session_id> [--json]
  aicode models [--json]
  aicode models probe [--no-tools] [--model <name>] [--json]
  aicode doctor [--json]
  aicode trust [status|add|remove|list] [--json]
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
				return options, nil, fmt.Errorf("用法: aicode --sandbox docker <test|build|lint>")
			}
			options.Sandbox = args[1]
			args = args[2:]
		default:
			return options, args, nil
		}
	}
	return options, args, nil
}
