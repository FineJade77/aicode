from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from app.agent.policy import PolicyEngine
from app.agent.ports import ToolSpec
from app.project.config import parse_project_config
from app.tools.base import ToolContext
from app.tools.mcp import McpManager, McpServerConfig, qualified_tool_name, spec_from_mcp
from app.tools.registry import build_default_registry

FAKE_SERVER = Path(__file__).with_name("mcp_fake_server.py")


def server_config(name: str = "fake", mode: str = "healthy", **kwargs) -> McpServerConfig:
    return McpServerConfig(name=name, command=[sys.executable, str(FAKE_SERVER), mode], **kwargs)


@pytest_asyncio.fixture
async def manager(tmp_path: Path):
    instance = McpManager(workspace=tmp_path)
    yield instance
    await instance.stop()


@pytest.mark.asyncio
async def test_tools_are_discovered_and_callable(manager, tmp_path: Path) -> None:
    tools = await manager.start([server_config()])

    assert [tool.spec.name for tool in tools] == ["mcp__fake__echo", "mcp__fake__shout"]
    echo = tools[0]
    result = await echo.run({"text": "hello"}, ToolContext(workspace=tmp_path))

    assert result.success is True
    assert "hello" in result.text
    # Non-text content is summarised rather than dropped silently.
    assert "[image content omitted]" in result.text
    assert result.data["mcp_server"] == "fake"


@pytest.mark.asyncio
async def test_external_tools_are_namespaced_and_cannot_shadow_builtins(manager, tmp_path: Path) -> None:
    """A server offering `bash` must not take over the name the policy layer has
    specific rules for."""
    tools = await manager.start([server_config()])
    registry = build_default_registry()
    for tool in tools:
        registry.register(tool)

    assert qualified_tool_name("fake", "bash") == "mcp__fake__bash"
    builtin = registry.spec_for("bash")
    assert builtin is not None and builtin.read_only is False
    assert registry.spec_for("mcp__fake__echo") is not None
    # Registering a server tool literally named `bash` still cannot collide.
    registry.register(_stub_tool(spec_from_mcp("fake", {"name": "bash"})))
    assert registry.spec_for("bash") is builtin


@pytest.mark.asyncio
async def test_external_tools_always_require_approval(manager) -> None:
    """A server's own claim to be read-only is unverifiable, so it is ignored.

    Believing it would skip the approval prompt for third-party code entirely.
    """
    tools = await manager.start([server_config()])
    engine = PolicyEngine()

    for tool in tools:
        assert tool.spec.read_only is False
        assert tool.spec.approval == "gate"
        assert engine.gate(tool.spec.name, {}, spec=tool.spec).verdict == "ask"
        # And they are refused outright in the read-only modes.
        assert engine.gate(tool.spec.name, {}, mode="review", spec=tool.spec).verdict == "deny"


def test_a_server_claiming_read_only_is_not_believed() -> None:
    spec = spec_from_mcp("fake", {"name": "peek", "readOnly": True, "annotations": {"readOnlyHint": True}})

    assert spec.read_only is False
    assert spec.approval == "gate"


def test_missing_description_is_synthesised() -> None:
    spec = spec_from_mcp("fake", {"name": "shout"})

    assert "shout" in spec.description
    assert "fake" in spec.description


@pytest.mark.asyncio
async def test_a_server_that_fails_to_start_does_not_stop_the_others(manager, tmp_path: Path) -> None:
    """An optional integration must not be able to take the Runtime down."""
    tools = await manager.start([server_config("broken", "crash"), server_config("good", "healthy")])

    assert [tool.spec.name for tool in tools] == ["mcp__good__echo", "mcp__good__shout"]
    status = manager.status()
    assert status["running"] == ["good"]
    assert "broken" in status["failed"]
    # The healthy server still works.
    result = await tools[0].run({"text": "ok"}, ToolContext(workspace=tmp_path))
    assert result.success is True


@pytest.mark.asyncio
async def test_a_protocol_violation_is_reported_as_a_start_failure(manager) -> None:
    tools = await manager.start([server_config("noisy", "garbage")])

    assert tools == []
    assert "noisy" in manager.status()["failed"]


@pytest.mark.asyncio
async def test_a_hanging_tool_call_times_out_as_a_tool_error(manager, tmp_path: Path) -> None:
    """A wedged server must surface as a tool error the model can react to, not
    as a stalled agent turn."""
    tools = await manager.start([server_config("slow", "hang", call_timeout=0.3)])

    result = await tools[0].run({"text": "hi"}, ToolContext(workspace=tmp_path))

    assert result.success is False
    assert "timeout" in result.data["status"]
    assert "slow" in result.error


@pytest.mark.asyncio
async def test_a_tool_reported_error_becomes_a_failed_tool_result(manager, tmp_path: Path) -> None:
    tools = await manager.start([server_config("failing", "error")])

    result = await tools[0].run({"text": "hi"}, ToolContext(workspace=tmp_path))

    assert result.success is False
    assert "tool failed" in result.error


@pytest.mark.asyncio
async def test_servers_do_not_inherit_provider_secrets(manager, tmp_path: Path, monkeypatch) -> None:
    """MCP servers are third-party subprocesses and go through the same minimal
    environment allowlist as every other command the Runtime spawns."""
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("AICODE_RUNTIME_TOKEN", "must-not-leak")
    monkeypatch.setenv("AICODE_HOME", str(tmp_path / "state"))
    leak_server = McpServerConfig(
        name="env",
        command=[
            sys.executable,
            "-c",
            "import json,os,sys\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if m.get('method') == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{}})+'\\n')\n"
            "    elif m.get('method') == 'tools/list':\n"
            "        leaked = [k for k in ('OPENAI_API_KEY','AICODE_RUNTIME_TOKEN') if os.environ.get(k)]\n"
            "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'tools':["
            "{'name':'leaked_'+('yes' if leaked else 'no')}]}})+'\\n')\n"
            "    else:\n"
            "        continue\n"
            "    sys.stdout.flush()\n",
        ],
    )

    tools = await manager.start([leak_server])

    assert [tool.spec.name for tool in tools] == ["mcp__env__leaked_no"]


@pytest.mark.asyncio
async def test_stop_terminates_every_server(tmp_path: Path) -> None:
    instance = McpManager(workspace=tmp_path)
    await instance.start([server_config("a"), server_config("b")])
    processes = [server._process for server in instance.servers]
    assert all(process is not None and process.returncode is None for process in processes)

    await instance.stop()

    assert instance.status()["running"] == []
    for process in processes:
        assert process is not None
        assert process.returncode is not None, "a killed daemon must not leave MCP servers behind"


def test_project_config_declares_servers() -> None:
    config = parse_project_config(
        {
            "mcp": {
                "servers": [
                    {"name": "files", "command": ["node", "server.js"], "envAllowlist": ["FILES_ROOT"]},
                    {"name": "files", "command": ["node", "dup.js"]},
                    {"name": "", "command": ["x"]},
                    {"name": "nocommand", "command": []},
                    "not-an-object",
                ]
            }
        }
    )

    # Duplicates and unusable entries are skipped rather than guessed at:
    # launching the wrong process is worse than launching none.
    assert [ref.name for ref in config.mcp_servers] == ["files"]
    assert config.mcp_servers[0].command == ["node", "server.js"]
    assert config.mcp_servers[0].env_allowlist == ("FILES_ROOT",)


def test_project_config_bounds_timeouts() -> None:
    config = parse_project_config(
        {
            "mcp": {
                "servers": [
                    {"name": "s", "command": ["x"], "startupTimeoutSeconds": 99999, "callTimeoutSeconds": -5},
                ]
            }
        }
    )

    assert config.mcp_servers[0].startup_timeout == 300.0
    assert config.mcp_servers[0].call_timeout == 1.0


def _stub_tool(spec: ToolSpec):
    class _Stub:
        def __init__(self) -> None:
            self.spec = spec

        async def run(self, args, context):  # pragma: no cover - never invoked
            raise AssertionError("stub tool must not run")

    return _Stub()


# --- wiring: from project config to a tool the model can call -----------------
#
# The manager and client were fully implemented and unit-tested, and connected to
# nothing: `McpManager` had no consumer in `app/`, the registry never saw an MCP
# tool, and the `mcp.server.*` events registered in `events.py` could not fire.
# These pin the connection itself, and the trust gate that guards it.

from app.tools.mcp.provider import McpToolProvider  # noqa: E402
from app.tools.runtime import DefaultToolRuntime  # noqa: E402


def provider_for(config: McpServerConfig) -> McpToolProvider:
    return McpToolProvider(lambda workspace: [config])


@pytest.mark.asyncio
async def test_a_trusted_workspace_gets_the_servers_tools(tmp_path: Path) -> None:
    runtime = DefaultToolRuntime(mcp=provider_for(server_config()))
    try:
        await runtime.prepare(str(tmp_path), trust_level="trusted")

        names = [schema["name"] for schema in runtime.schemas_for_mode("default", workspace=str(tmp_path))]

        assert any(name.startswith("mcp__fake__") for name in names), names
    finally:
        await runtime.mcp.aclose()


@pytest.mark.asyncio
async def test_an_untrusted_workspace_starts_no_servers(tmp_path: Path) -> None:
    """The server list comes from the repository's own config.

    An untrusted checkout naming a process for the daemon to launch is the exact
    power `.aicode` hooks already refuse, so MCP gives the same answer instead of
    inventing a second one.
    """
    runtime = DefaultToolRuntime(mcp=provider_for(server_config()))
    try:
        await runtime.prepare(str(tmp_path), trust_level="untrusted")

        names = [schema["name"] for schema in runtime.schemas_for_mode("default", workspace=str(tmp_path))]

        assert not any(name.startswith("mcp__") for name in names), names
        assert runtime.mcp.status() == {}
    finally:
        await runtime.mcp.aclose()


@pytest.mark.asyncio
async def test_an_external_tool_runs_through_the_tool_runtime(tmp_path: Path) -> None:
    runtime = DefaultToolRuntime(mcp=provider_for(server_config()))
    try:
        await runtime.prepare(str(tmp_path), trust_level="trusted")
        name = next(
            schema["name"]
            for schema in runtime.schemas_for_mode("default", workspace=str(tmp_path))
            if schema["name"].startswith("mcp__fake__")
        )

        spec = runtime.spec_for(name, workspace=str(tmp_path))
        result = await runtime.run(name, {"value": "hi"}, ToolContext(workspace=tmp_path))

        # Forced regardless of what the server claims about itself.
        assert spec is not None and spec.read_only is False and spec.approval == "gate"
        assert result.success is True
    finally:
        await runtime.mcp.aclose()


@pytest.mark.asyncio
async def test_servers_start_once_and_are_reused_across_runs(tmp_path: Path) -> None:
    """Servers are subprocesses with a handshake; per-run startup would pay it every message."""
    starts = 0

    def configs_for(workspace: Path):
        nonlocal starts
        starts += 1
        return [server_config()]

    runtime = DefaultToolRuntime(mcp=McpToolProvider(configs_for))
    try:
        await runtime.prepare(str(tmp_path), trust_level="trusted")
        await runtime.prepare(str(tmp_path), trust_level="trusted")

        assert starts == 1
    finally:
        await runtime.mcp.aclose()


@pytest.mark.asyncio
async def test_a_failing_server_is_reported_and_leaves_the_run_working(tmp_path: Path) -> None:
    events: list[tuple[str, str, dict]] = []
    runtime = DefaultToolRuntime(mcp=provider_for(server_config(mode="crash")))
    try:
        await runtime.prepare(
            str(tmp_path),
            trust_level="trusted",
            on_event=lambda name, status, data: events.append((name, status, data)),
        )

        assert [status for _, status, _ in events] == ["failed"]
        # The built-in tools must still be there: an optional integration cannot
        # take the turn down with it.
        names = [schema["name"] for schema in runtime.schemas_for_mode("default", workspace=str(tmp_path))]
        assert "read_file" in names
    finally:
        await runtime.mcp.aclose()


@pytest.mark.asyncio
async def test_a_workspace_without_servers_costs_nothing(tmp_path: Path) -> None:
    runtime = DefaultToolRuntime(mcp=McpToolProvider(lambda workspace: []))
    await runtime.prepare(str(tmp_path), trust_level="trusted")

    assert runtime.mcp.status() == {}
    assert runtime.spec_for("read_file", workspace=str(tmp_path)) is not None


# --- HTTP transport -----------------------------------------------------------
#
# The protocol, not just the client: a server may answer a request with a plain
# JSON body or with an SSE stream, may assign a session that has to be echoed
# back, and is otherwise attacker-controlled bytes arriving over the network.

import httpx  # noqa: E402

from app.tools.mcp.http import MAX_RESPONSE_BYTES, HttpMcpServer  # noqa: E402
from app.tools.mcp.protocol import McpProtocolError  # noqa: E402

TOOL_DESCRIPTOR = {"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}


def json_rpc_result(request_body: bytes, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": json.loads(request_body)["id"], "result": result}


def scripted_transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def default_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    method = body.get("method")
    if "id" not in body:
        return httpx.Response(202)
    if method == "initialize":
        return httpx.Response(
            200,
            json=json_rpc_result(request.content, {"protocolVersion": "2025-06-18"}),
            headers={"content-type": "application/json", "mcp-session-id": "sess-123"},
        )
    if method == "tools/list":
        return httpx.Response(
            200,
            json=json_rpc_result(request.content, {"tools": [TOOL_DESCRIPTOR]}),
            headers={"content-type": "application/json"},
        )
    return httpx.Response(
        200,
        json=json_rpc_result(request.content, {"content": [{"type": "text", "text": "pong"}]}),
        headers={"content-type": "application/json"},
    )


@pytest.mark.asyncio
async def test_http_handshake_lists_and_calls_tools() -> None:
    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(default_handler))

    descriptors = await server.start()
    result = await server.call_tool("echo", {"value": "hi"})

    assert [descriptor["name"] for descriptor in descriptors] == ["echo"]
    assert result["content"][0]["text"] == "pong"
    await server.stop()


@pytest.mark.asyncio
async def test_the_session_id_is_echoed_on_later_requests() -> None:
    """The server assigns it on initialize; dropping it restarts the conversation."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("mcp-session-id", ""))
        return default_handler(request)

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))
    await server.start()
    await server.stop()

    assert seen[0] == ""
    assert all(value == "sess-123" for value in seen[1:]), seen


@pytest.mark.asyncio
async def test_a_response_may_arrive_as_an_sse_stream() -> None:
    """The other legal shape. A client that only handles JSON silently fails here."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        payload = json.dumps(json_rpc_result(request.content, {"tools": [TOOL_DESCRIPTOR]}))
        if body["method"] == "initialize":
            return httpx.Response(200, json=json_rpc_result(request.content, {}), headers={"content-type": "application/json"})
        return httpx.Response(
            200,
            text=f"event: message\ndata: {payload}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    descriptors = await server.start()

    assert [descriptor["name"] for descriptor in descriptors] == ["echo"]
    await server.stop()


@pytest.mark.asyncio
async def test_interleaved_stream_frames_are_skipped_until_the_answer() -> None:
    """Servers may push notifications before the response; the id is what ends the read."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json=json_rpc_result(request.content, {}), headers={"content-type": "application/json"})
        noise = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
        answer = json.dumps(json_rpc_result(request.content, {"tools": [TOOL_DESCRIPTOR]}))
        return httpx.Response(
            200,
            text=f"data: {noise}\n\ndata: not-json\n\ndata: {answer}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    assert [descriptor["name"] for descriptor in await server.start()] == ["echo"]
    await server.stop()


@pytest.mark.asyncio
async def test_a_stream_that_never_answers_is_an_error_not_a_hang() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: {}\n\n", headers={"content-type": "text/event-stream"})

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    with pytest.raises(McpProtocolError, match="without answering"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_a_redirect_is_refused_rather_than_followed() -> None:
    """Following one would re-send the Authorization header to another host."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://elsewhere.example/rpc"})

    server = HttpMcpServer(
        "remote", "https://mcp.example/rpc", auth_token_env="MCP_TOKEN", client=scripted_transport(handler)
    )

    with pytest.raises(McpProtocolError, match="redirect"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_the_bearer_token_comes_from_the_environment_not_the_config(monkeypatch) -> None:
    """A secret in `.aicode/config.json` is a secret whoever declared it commits."""
    monkeypatch.setenv("MCP_TOKEN", "s3cret")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return default_handler(request)

    server = HttpMcpServer(
        "remote", "https://mcp.example/rpc", auth_token_env="MCP_TOKEN", client=scripted_transport(handler)
    )
    await server.start()
    await server.stop()

    assert seen and all(value == "Bearer s3cret" for value in seen)


@pytest.mark.asyncio
async def test_no_authorization_header_without_a_configured_env_var() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return default_handler(request)

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))
    await server.start()
    await server.stop()

    assert seen and all(value == "" for value in seen)


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (MAX_RESPONSE_BYTES + 1),
            headers={"content-type": "application/json"},
        )

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    with pytest.raises(McpProtocolError, match="more than"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_an_http_error_status_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    with pytest.raises(McpProtocolError, match="HTTP 503"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_an_unexpected_content_type_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html/>", headers={"content-type": "text/html"})

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    with pytest.raises(McpProtocolError, match="content type"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_a_json_rpc_error_becomes_a_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32000, "message": "nope"}},
            headers={"content-type": "application/json"},
        )

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    with pytest.raises(McpProtocolError, match="nope"):
        await server.start()
    await server.stop()


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_a_timeout() -> None:
    """The tool layer maps TimeoutError to a distinct message; a generic error would lose that."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    server = HttpMcpServer("remote", "https://mcp.example/rpc", client=scripted_transport(handler))

    with pytest.raises(TimeoutError):
        await server.start()
    await server.stop()


def test_a_non_http_url_is_refused_at_construction() -> None:
    for url in ("file:///etc/passwd", "ftp://example/rpc", "/local/path"):
        with pytest.raises(McpProtocolError, match="http or https"):
            HttpMcpServer("remote", url)


@pytest.mark.asyncio
async def test_declaring_both_transports_fails_that_server_only(tmp_path: Path) -> None:
    """Guessing which one was meant would start something nobody asked for."""
    instance = McpManager(workspace=tmp_path)
    ambiguous = McpServerConfig(name="both", command=["true"], url="https://mcp.example/rpc")
    events: list[tuple[str, str, dict]] = []

    tools = await instance.start(
        [ambiguous, server_config(name="fine")],
        on_event=lambda name, status, data: events.append((name, status, data)),
    )
    await instance.stop()

    assert "both" in instance.failures
    assert any(tool.spec.name.startswith("mcp__fine__") for tool in tools)


def test_project_config_accepts_either_transport_but_not_both() -> None:
    config = parse_project_config(
        {
            "mcp": {
                "servers": [
                    {"name": "local", "command": ["run-me"]},
                    {"name": "remote", "url": "https://mcp.example/rpc", "authTokenEnv": "MCP_TOKEN"},
                    {"name": "both", "command": ["run-me"], "url": "https://mcp.example/rpc"},
                    {"name": "neither"},
                ]
            }
        }
    )

    names = [ref.name for ref in config.mcp_servers]
    assert names == ["local", "remote"]
    assert config.mcp_servers[1].url == "https://mcp.example/rpc"
    assert config.mcp_servers[1].auth_token_env == "MCP_TOKEN"
