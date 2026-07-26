# Local Provider Profiles

`aicode` 把本地 OpenAI-compatible endpoint 表达为版本化 Provider Profile，而不是要求用户为本地服务伪造 API key。

Profile v1 字段：

| 字段 | 说明 |
| --- | --- |
| `profile` / `profile_schema_version` | 可读名称与契约版本；当前版本固定为 `1` |
| `base_url` | 必须包含 OpenAI-compatible `/v1` 前缀 |
| `auth_mode` | `required`、`optional` 或 `none` |
| `models.main/reviewer/summarizer` | 各 purpose 使用的服务端模型 ID |
| `context_window` / `max_output_tokens` | Agent compaction 与请求输出预算 |
| `tool_calling` | 是否声明支持原生 `tools` / `tool_calls` |
| `streaming` | 是否声明支持 SSE streaming；当前 Agent 必须为 `true` |
| `tokenizer` / `chars_per_token` | token 估算策略；v1 支持 `chars` |

`auth_mode=none` 时 Runtime 永远不发送 `Authorization`，即使父进程恰好存在 `OPENAI_API_KEY`；`optional` 只在 key 存在时发送；`required` 缺少 key 会快速失败。

## Probe

```bash
aicode models probe
aicode models probe --json
aicode models probe --model YOUR_MODEL_ID
aicode models probe --no-tools
```

默认 probe 依次检查：

1. profile/auth 配置；
2. `GET /v1/models` 可达且返回 OpenAI-compatible JSON；
3. 配置模型 ID 确实存在；
4. `POST /v1/chat/completions` 能产生完整 SSE；
5. 模型返回指定的原生 `tool_calls`。

失败会分类为 `auth_required`、`auth_failed`、`endpoint_unreachable`、`endpoint_http_error`、`invalid_models_response`、`model_not_found`、`streaming_failed` 或 `tools_unsupported`。`--no-tools` 仅用于诊断纯流式模型；Coding Agent 的正常模式仍需要原生 tools。系统不会把模型正文当 JSON 猜测。

修改 profile 后重启 Runtime：

```bash
aicode daemon stop
aicode daemon start
aicode models probe
```

## Ollama

[Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility) 暴露 `/v1/models`、streaming Chat Completions 和 tools。先拉取一个明确支持 tool calling 的模型，然后把 `YOUR_MODEL_ID` 替换为 `ollama list` / probe 返回的精确 ID。

```bash
aicode config set provider.type openai_compatible
aicode config set provider.openai_compatible.profile ollama
aicode config set provider.openai_compatible.profile_schema_version 1
aicode config set provider.openai_compatible.base_url http://127.0.0.1:11434/v1
aicode config set provider.openai_compatible.auth_mode none
aicode config set provider.openai_compatible.context_window 32768
aicode config set provider.openai_compatible.max_output_tokens 4096
aicode config set provider.openai_compatible.tool_calling true
aicode config set provider.openai_compatible.streaming true
aicode config set provider.openai_compatible.tokenizer chars
aicode config set provider.openai_compatible.chars_per_token 3.5
aicode config set models.main YOUR_MODEL_ID
aicode config set models.reviewer YOUR_MODEL_ID
aicode config set models.summarizer YOUR_MODEL_ID
```

Ollama 的 context size 由服务端模型配置决定；Profile 必须与实际 `num_ctx` 保持一致，不能靠客户端字段扩大服务端窗口。

## llama.cpp server

[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) 提供 `/v1/models` 和 streaming `/v1/chat/completions`。启动时建议设置稳定 alias，并为 function calling 启用兼容的 Jinja chat template：

```bash
llama-server -m /path/to/model.gguf --alias local-coder --jinja --ctx-size 32768 --port 8080

aicode config set provider.type openai_compatible
aicode config set provider.openai_compatible.profile llama_cpp
aicode config set provider.openai_compatible.profile_schema_version 1
aicode config set provider.openai_compatible.base_url http://127.0.0.1:8080/v1
aicode config set provider.openai_compatible.auth_mode none
aicode config set provider.openai_compatible.context_window 32768
aicode config set provider.openai_compatible.max_output_tokens 4096
aicode config set provider.openai_compatible.tool_calling true
aicode config set provider.openai_compatible.streaming true
aicode config set models.main local-coder
aicode config set models.reviewer local-coder
aicode config set models.summarizer local-coder
```

模型 chat template 不支持 tools 时，profile 不应谎报 `tool_calling=true`；probe 会要求真实 `tool_calls`，只输出一段 JSON 文本仍判定失败。

## LM Studio

[LM Studio Local Server](https://lmstudio.ai/docs/developer/rest/quickstart) 默认监听 `1234` 且默认不要求认证；其 [OpenAI-compatible tool streaming](https://lmstudio.ai/docs/developer/openai-compat/tools) 会在 SSE delta 中返回分片 tool call。

```bash
aicode config set provider.type openai_compatible
aicode config set provider.openai_compatible.profile lm_studio
aicode config set provider.openai_compatible.profile_schema_version 1
aicode config set provider.openai_compatible.base_url http://127.0.0.1:1234/v1
aicode config set provider.openai_compatible.auth_mode none
aicode config set provider.openai_compatible.context_window 32768
aicode config set provider.openai_compatible.max_output_tokens 4096
aicode config set provider.openai_compatible.tool_calling true
aicode config set provider.openai_compatible.streaming true
aicode config set models.main YOUR_MODEL_ID
aicode config set models.reviewer YOUR_MODEL_ID
aicode config set models.summarizer YOUR_MODEL_ID
```

如果在 LM Studio 中启用了 Require Authentication，改用 `required` 并配置 key 来源：

```bash
aicode config set provider.openai_compatible.auth_mode required
aicode config set provider.openai_compatible.api_key_env LM_API_TOKEN
export LM_API_TOKEN="..."
```

## 验证矩阵

截至 2026-07-26：

| 路径 | 状态 |
| --- | --- |
| Profile v1、none/optional/required、`/models`、SSE、分片 tools | 自动化测试 |
| no-auth localhost read → edit proposal → approval → verify | 真实 TCP HTTP smoke，自动化测试 |
| Ollama / llama.cpp / LM Studio 配置模板 | 按各项目官方当前文档编写；本机未安装，因此不声明具体产品版本已手工验证 |

CI 通过 `make test-local-provider-smoke` 运行 localhost smoke。接入任何具体模型或服务版本时，必须保存 `aicode models probe --json` 结果，并以实际模型 ID、context 配置和 native tools 结果为准。
