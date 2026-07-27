# Contract Fixtures

这些 fixture 是 Python Runtime 与 Go CLI 共享的 v2 contract 样本。

- `http-responses.v2.json`：CLI 使用的关键 HTTP response，包括可空的 `cancel_run.run_id`、execution 和 Project Trust。
- `sse-events.v2.json`：所有 v2 Runtime event、终态顺序和前向兼容样本。
- `application-contract.v2.json`：不含语言字段的 Application Runtime 具名契约样本。

兼容规则：

1. 同一 major contract version 内可以增加 JSON 字段；客户端必须忽略不认识的字段。
2. Runtime 只发送 `events.schema.json` 登记的 v2 event type。
3. Go client 收到未来版本的未知 event type 时应透传给 handler，不提前结束 stream。
4. Go renderer 对未知 event type 回显 JSON，避免静默丢失信息。
5. 只有 payload 中 `type == "final"` 的 event 结束当前 SSE stream。

修改 HTTP response、event、Go client 或 renderer 时，应同步 fixture 和对应 contract tests。
