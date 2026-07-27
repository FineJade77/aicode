"""No-auth localhost OpenAI-compatible profile smoke and probe classification."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters.approvals import SessionApprovalBroker
from app.adapters.system import SystemClock
from app.adapters.tools import DefaultToolRuntime
from app.adapters.workspace import LocalWorkspaceRuntime
from app.agent.loop import run_turn_safely
from app.agent.types import AgentRuntime
from app.audit.logger import AuditLogger
from app.config.settings import ModelSettings, OpenAICompatibleSettings, Settings
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.router import ModelRouter
from app.policy.engine import PolicyEngine
from app.sessions.store import SessionStore


class Request:
    def __init__(self, workspace: Path) -> None:
        self.workspace = str(workspace)
        self.message = "Fix add in calc.py and verify the change"
        self.mode = "default"
        self.language = "en-US"


class LocalProviderFixture:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.agent_calls = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                fixture.requests.append({"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization")})
                if self.path == "/v1/models":
                    self._json(200, {"object": "list", "data": [{"id": "local-coder", "object": "model"}]})
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length))
                fixture.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "payload": payload,
                    }
                )
                if self.path != "/v1/chat/completions":
                    self._json(404, {"error": "not found"})
                    return
                user_text = " ".join(
                    str(message.get("content") or "")
                    for message in payload.get("messages") or []
                    if message.get("role") == "user"
                )
                if "aicode_probe" in user_text:
                    self._sse(tool_chunk("probe_1", "aicode_probe", {"ok": True}))
                    return

                fixture.agent_calls += 1
                responses = {
                    1: tool_chunk("tc_read", "read_file", {"path": "calc.py"}),
                    2: tool_chunk(
                        "tc_edit",
                        "edit_file",
                        {
                            "path": "calc.py",
                            "old_text": "return a - b",
                            "new_text": "return a + b",
                        },
                    ),
                    3: tool_chunk("tc_verify", "bash", {"command": "python3 -m py_compile calc.py"}),
                    4: text_chunk("The fix is complete."),
                    5: text_chunk("Verified with py_compile."),
                }
                self._sse(responses[fixture.agent_calls])

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sse(self, chunk: dict[str, Any]) -> None:
                chunks = [
                    chunk,
                    {
                        "model": "local-coder",
                        "choices": [{"delta": {}}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                    },
                ]
                body = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
                body += "data: [DONE]\n\n"
                encoded = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def tool_chunk(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": "local-coder",
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ]
                }
            }
        ],
    }


def text_chunk(text: str) -> dict[str, Any]:
    return {"model": "local-coder", "choices": [{"delta": {"content": text}}]}


@pytest.mark.asyncio
async def test_no_auth_localhost_profile_probe_and_full_edit_flow(tmp_path: Path) -> None:
    try:
        fixture = LocalProviderFixture()
    except PermissionError:
        pytest.skip("sandbox does not allow binding a localhost smoke server")
    fixture.start()
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    settings = Settings(
        models=ModelSettings(main="local-coder", reviewer="local-coder", summarizer="local-coder"),
        openai_compatible=OpenAICompatibleSettings(
            profile="local-fixture",
            base_url=fixture.base_url,
            auth_mode="none",
            api_key_env="",
            context_window=16_384,
            max_output_tokens=2_048,
            tool_calling=True,
            streaming=True,
        ),
    )
    router = ModelRouter.from_settings(settings)
    audit = AuditLogger(path=tmp_path / "audit.jsonl")
    runtime = AgentRuntime(
        model_router=router,
        audit=audit,
        policy=PolicyEngine(),
        tools=DefaultToolRuntime(),
        workspace=LocalWorkspaceRuntime(),
        clock=SystemClock(),
        approvals=SessionApprovalBroker(),
    )
    store = SessionStore(path=tmp_path / "sessions.sqlite")
    session = store.create(workspace=str(tmp_path), language="en-US")

    async def approve_pending() -> None:
        while True:
            await asyncio.sleep(0.01)
            for approval in list(session.approvals.values()):
                if approval.accepted is None:
                    session.resolve_approval(approval.approval_id, accepted=True)

    approver: asyncio.Task[None] | None = None
    try:
        probe = await asyncio.wait_for(router.probe(), timeout=5)
        assert probe["status"] == "ok"
        assert [check["code"] for check in probe["checks"]] == [
            "configured",
            "reachable",
            "model_found",
            "stream_ok",
            "tools_ok",
        ]

        approver = asyncio.create_task(approve_pending())
        await asyncio.wait_for(run_turn_safely(session, Request(tmp_path), runtime), timeout=15)
    finally:
        if approver is not None and not approver.done():
            approver.cancel()
        await router.aclose()
        await audit.aclose()
        await store.aclose()
        fixture.close()

    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    event_types = [event["type"] for event in session.events.events_after(0)]
    for expected in ("approval.requested", "edit.applied", "final"):
        assert expected in event_types
    assert fixture.agent_calls == 5
    assert fixture.requests
    assert all(request["authorization"] is None for request in fixture.requests)
    assert all(
        request["payload"]["model"] == "local-coder"
        for request in fixture.requests
        if request["method"] == "POST"
    )


@pytest.mark.asyncio
async def test_probe_classifies_missing_model() -> None:
    settings = OpenAICompatibleSettings(
        profile="local",
        base_url="http://local.invalid/v1",
        auth_mode="none",
        api_key_env="",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"data": [{"id": "another-model"}]})
        )
    )
    provider = OpenAICompatibleProvider(settings, client=client)
    try:
        result = await provider.probe("missing-model")
    finally:
        await provider.aclose()

    assert result["status"] == "error"
    assert result["checks"][-1]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_probe_classifies_unreachable_endpoint() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    settings = OpenAICompatibleSettings(
        profile="local",
        base_url="http://127.0.0.1:1/v1",
        auth_mode="none",
        api_key_env="",
    )
    provider = OpenAICompatibleProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(unreachable)))
    try:
        result = await provider.probe("local-model")
    finally:
        await provider.aclose()

    assert result["status"] == "error"
    assert result["checks"][-1]["code"] == "endpoint_unreachable"


@pytest.mark.asyncio
async def test_probe_fails_when_model_returns_text_instead_of_native_tool_call() -> None:
    stream = (
        'data: {"model":"local-model","choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}\n\n'
        'data: {"model":"local-model","choices":[{"delta":{}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        return httpx.Response(200, content=stream)

    settings = OpenAICompatibleSettings(
        profile="local",
        base_url="http://local.invalid/v1",
        auth_mode="none",
        api_key_env="",
    )
    provider = OpenAICompatibleProvider(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        result = await provider.probe("local-model")
    finally:
        await provider.aclose()

    assert result["status"] == "error"
    assert result["checks"][-1]["code"] == "tools_unsupported"
    assert "guess JSON" in result["checks"][-1]["summary"]
