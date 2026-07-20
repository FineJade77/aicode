# Daemon Stability Iteration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the aicode Runtime daemon safe and stable to run long-lived: streaming `assistant.delta` events stop blocking the event loop and stop crowding out real history, event persistence moves off the synchronous hot path, the local HTTP API requires a token so another local process can't forge approvals, and the in-memory session cache stops growing without bound.

**Architecture:** Four independent, sequentially-landed fixes against the existing v2 agent-loop runtime (Python FastAPI daemon + Go CLI). No new services, no new dependencies beyond the Python/Go standard libraries already vendored (`hmac`, `crypto/rand`, `encoding/hex`) plus `httpx.ASGITransport` (httpx is already a project dependency) for in-process auth testing. Each task lands, is tested, and is committed before the next starts.

**Tech Stack:** Python 3.11+ (FastAPI, `asyncio`, `sqlite3`), Go (stdlib `net/http`, `crypto/rand`), pytest + pytest-asyncio, `go test`.

## Global Constraints

- Python 3.11+; no new runtime dependencies (stdlib only for this plan's Python changes; `httpx` is already a dependency, used only in tests here).
- Go stdlib only (`crypto/rand`, `encoding/hex`) — no new Go modules.
- Every commit must leave the full suite green: `cd runtime && python3 -m pytest -q` (baseline 152 passing) and `go test ./cli/...` (from repo root) at the end of every task.
- User-visible CLI text follows existing localization conventions (`localized()` in `runtime/app/agent/utils.py`) where the plan touches user-facing strings; this plan's new strings are operator-facing (daemon start/stop, auth errors) and follow existing Chinese-first phrasing already used in `cli/main.go` and `daemon.go`.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Follow existing patterns: Python tests use `pytest.mark.asyncio` + `tmp_path` fixtures matching `runtime/tests/test_sessions.py`; Go tests use `httptest`/`roundTripFunc` matching `cli/internal/client/client_test.go`.
- Do not touch unrelated code. `runtime/app/sessions/store.py`, `runtime/app/server/main.py`, `cli/internal/daemon/daemon.go`, `cli/internal/client/client.go` are the only production files this plan modifies (plus one new Python module and one new Go test file).

## File Structure (final state)

```
runtime/app/
  sessions/store.py       # Modify: transient event handling, async write-behind, LRU cache eviction
  server/
    auth.py                # New: token-based auth middleware
    main.py                 # Modify: wire auth middleware, flush() on shutdown
runtime/tests/
  test_sessions.py          # Modify: fix 2 tests made racy by async write-behind; add new tests
  test_server_auth.py       # New: auth middleware tests (httpx.ASGITransport)
cli/internal/
  daemon/
    daemon.go               # Modify: generate/persist/remove token on Start/Stop; Token() reader
    daemon_test.go           # New: token generation/read tests
  client/
    client.go               # Modify: Client carries token, sends Authorization header
    client_test.go           # Modify: New() signature change; add auth-header test
cli/main.go                  # Modify: 4 call sites pass daemon.Token() to client.New
README.md                    # Modify: document AICODE_RUNTIME_TOKEN, AICODE_SESSION_CACHE_LIMIT
```

---

### Task 1: `assistant.delta` events are not persisted and cannot crowd out real history

**Files:**
- Modify: `runtime/app/sessions/store.py`
- Test: `runtime/tests/test_sessions.py`

**Interfaces:**
- Consumes: existing `SessionEvents.__init__(events, on_event, max_events)`, `SessionEvents.put(event)`, `SessionEvents.events_after(after)` — no signature changes.
- Produces: new module constant `MAX_TRANSIENT_RETAINED_EVENTS = 200`; `SessionEvents.put()` skips the `on_event` callback (no DB write) for events whose `type == "assistant.delta"`, but still appends them to the live in-memory buffer so active SSE subscribers keep receiving them; a new private method `_trim_transient_events()` caps how many `assistant.delta` entries stay in the in-memory buffer, independent of the existing `_max_events` budget for everything else. Later tasks do not depend on this interface.

- [ ] **Step 1: Write the failing tests**

Add to `runtime/tests/test_sessions.py` (after the existing `test_session_events_trim_retained_events` test, before `test_session_store_prunes_persisted_events`):

```python
@pytest.mark.asyncio
async def test_assistant_delta_is_not_persisted() -> None:
    persisted: list[dict] = []
    events = SessionEvents(on_event=persisted.append)

    await events.put({"type": "assistant.delta", "text": "他"})
    await events.put({"type": "final", "summary": "done"})

    assert [call["type"] for call in persisted] == ["final"]


@pytest.mark.asyncio
async def test_assistant_delta_still_delivered_to_live_subscribers() -> None:
    events = SessionEvents()

    await events.put({"type": "assistant.delta", "text": "你"})
    await events.put({"type": "assistant.delta", "text": "好"})

    retained = events.events_after(0)

    assert [event.get("text") for event in retained] == ["你", "好"]


@pytest.mark.asyncio
async def test_assistant_delta_flood_does_not_evict_real_events() -> None:
    events = SessionEvents(max_events=250)
    await events.put({"type": "tool.started", "tool": "read_file"})

    for _ in range(300):
        await events.put({"type": "assistant.delta", "text": "x"})

    retained = events.events_after(0)
    types = [event["type"] for event in retained]

    assert "tool.started" in types
    assert types.count("assistant.delta") <= 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd runtime && python3 -m pytest tests/test_sessions.py -k "assistant_delta" -v`
Expected: FAIL — `test_assistant_delta_is_not_persisted` fails because `on_event` is currently called for every event including `assistant.delta`; `test_assistant_delta_flood_does_not_evict_real_events` fails because the flood (300 deltas + 1 tool.started = 301 events) exceeds the pre-fix single FIFO budget of `max_events=250` and evicts the oldest event, `tool.started`.

- [ ] **Step 3: Implement**

In `runtime/app/sessions/store.py`, add the constant near `DEFAULT_SESSION_EVENT_LIMIT`:

```python
DEFAULT_SESSION_EVENT_LIMIT = 2_000
MAX_TRANSIENT_RETAINED_EVENTS = 200
```

Modify `SessionEvents.put`:

```python
    async def put(self, event: dict[str, Any]) -> None:
        event = dict(event)
        if self._current_run_id and "run_id" not in event:
            event["run_id"] = self._current_run_id
        if "event_id" not in event:
            event["event_id"] = self._next_sequence
            self._next_sequence += 1
        else:
            self._next_sequence = max(self._next_sequence, int(event["event_id"]) + 1)

        if self._on_event is not None and event.get("type") != "assistant.delta":
            try:
                self._on_event(event)
            except Exception:
                pass

        async with self._condition:
            self._events.append(event)
            self._trim_retained_events()
            self._condition.notify_all()
```

Modify `_trim_retained_events` and add `_trim_transient_events` right after it:

```python
    def _trim_retained_events(self) -> None:
        self._trim_transient_events()
        if len(self._events) <= self._max_events:
            return
        del self._events[: len(self._events) - self._max_events]

    def _trim_transient_events(self) -> None:
        transient_indexes = [index for index, event in enumerate(self._events) if event.get("type") == "assistant.delta"]
        excess = len(transient_indexes) - MAX_TRANSIENT_RETAINED_EVENTS
        if excess <= 0:
            return
        drop = set(transient_indexes[:excess])
        self._events = [event for index, event in enumerate(self._events) if index not in drop]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd runtime && python3 -m pytest tests/test_sessions.py -v`
Expected: all tests in the file PASS, including the 3 new ones and all pre-existing ones (this confirms `max_events=3` and other small-budget tests, which never use `assistant.delta`, are unaffected — `_trim_transient_events` is a no-op when there are 0 transient events).

Run full suite: `cd runtime && python3 -m pytest -q`
Expected: `155 passed` (152 baseline + 3 new).

- [ ] **Step 5: Commit**

```bash
git add runtime/app/sessions/store.py runtime/tests/test_sessions.py
git commit -m "Stop persisting assistant.delta events and cap their retained count

assistant.delta fires once per streamed token chunk during a turn. Writing
each one to SQLite blocked the event loop for no benefit (the final event
already carries the complete text), and a long turn's delta flood could
evict genuinely important history (approval.requested, edit.applied) from
the bounded in-memory retention buffer. Deltas are still delivered live to
active SSE subscribers; they're capped at 200 retained entries independent
of the main event budget so they can no longer starve it.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Event persistence moves off the request-handling hot path

**Files:**
- Modify: `runtime/app/sessions/store.py`
- Modify: `runtime/app/server/main.py`
- Test: `runtime/tests/test_sessions.py`

**Interfaces:**
- Consumes: Task 1's unchanged `SessionEvents`/`SessionStore` shape; existing `store` singleton and `lifespan` context manager in `main.py`.
- Produces: `SessionStore._append_event(session_id, event)` becomes non-blocking (enqueues instead of writing synchronously); new public `async def SessionStore.flush() -> None` awaits until all queued events have been written (or attempted) — used by tests and by the FastAPI `lifespan` shutdown hook so no in-flight event is silently dropped on a clean shutdown. Later tasks do not depend on new internals beyond `flush()`.

- [ ] **Step 1: Fix the two existing tests this change makes racy, then write the new failing tests**

The existing `test_session_store_persists_events` and `test_session_store_prunes_persisted_events` in `runtime/tests/test_sessions.py` currently read the SQLite file synchronously right after `await session.events.put(...)`, relying on the write being synchronous. Once persistence is async, that read can race the background writer. Update both to await `store.flush()` first — find and replace:

```python
@pytest.mark.asyncio
async def test_session_store_persists_events(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    await session.events.put({"type": "plan.created"})
    await session.events.put({"type": "final", "summary": "done"})

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    events = restored.events.events_after(0)
    assert [event["type"] for event in events] == ["plan.created", "final"]
    assert [event["event_id"] for event in events] == [1, 2]
```

becomes:

```python
@pytest.mark.asyncio
async def test_session_store_persists_events(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    await session.events.put({"type": "plan.created"})
    await session.events.put({"type": "final", "summary": "done"})
    await store.flush()

    reloaded = SessionStore(db_path)
    restored = reloaded.get(session.session_id)

    assert restored is not None
    events = restored.events.events_after(0)
    assert [event["type"] for event in events] == ["plan.created", "final"]
    assert [event["event_id"] for event in events] == [1, 2]
```

And:

```python
@pytest.mark.asyncio
async def test_session_store_prunes_persisted_events(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path, event_limit=2)
    session = store.create(workspace="/repo", language="zh-CN")

    for index in range(4):
        await session.events.put({"type": f"event.{index}"})

    with sqlite3.connect(db_path) as conn:
```

becomes:

```python
@pytest.mark.asyncio
async def test_session_store_prunes_persisted_events(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path, event_limit=2)
    session = store.create(workspace="/repo", language="zh-CN")

    for index in range(4):
        await session.events.put({"type": f"event.{index}"})
    await store.flush()

    with sqlite3.connect(db_path) as conn:
```

Now add new tests, after `test_session_store_prunes_persisted_events` (before `test_normalize_event_limit_uses_minimum_one`):

```python
@pytest.mark.asyncio
async def test_flush_waits_for_pending_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    await session.events.put({"type": "tool.started", "tool": "read_file"})
    await store.flush()

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "select count(*) from events where session_id = ?", (session.session_id,)
        ).fetchone()[0]

    assert count == 1


@pytest.mark.asyncio
async def test_event_writer_survives_individual_write_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "sessions.sqlite"
    store = SessionStore(db_path)
    session = store.create(workspace="/repo", language="zh-CN")

    original_write = store._write_event_sync
    call_count = {"value": 0}

    def flaky_write(session_id: str, event: dict) -> None:
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("simulated disk error")
        original_write(session_id, event)

    monkeypatch.setattr(store, "_write_event_sync", flaky_write)

    await session.events.put({"type": "one"})
    await session.events.put({"type": "two"})
    await store.flush()

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "select count(*) from events where session_id = ?", (session.session_id,)
        ).fetchone()[0]

    # 第一条写入失败被吞掉（不中断写入循环），第二条成功落盘
    assert count == 1


@pytest.mark.asyncio
async def test_flush_is_a_noop_when_nothing_was_ever_written(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    # 从未 put 过事件，writer 从未启动；flush 不应抛错或挂起
    await store.flush()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd runtime && python3 -m pytest tests/test_sessions.py -v`
Expected: `test_flush_waits_for_pending_writes`, `test_event_writer_survives_individual_write_failures`, `test_flush_is_a_noop_when_nothing_was_ever_written` FAIL with `AttributeError: 'SessionStore' object has no attribute 'flush'` (and `'_write_event_sync'`). The two edited pre-existing tests should still PASS at this point in the sense that nothing about persistence changed yet, but they now also call `store.flush()` which doesn't exist yet — so they FAIL too with the same `AttributeError`. That is the expected RED for this step; both become GREEN once Step 3 lands.

- [ ] **Step 3: Implement**

In `runtime/app/sessions/store.py`, add the queue size constant near the top:

```python
DEFAULT_SESSION_EVENT_LIMIT = 2_000
MAX_TRANSIENT_RETAINED_EVENTS = 200
EVENT_WRITE_QUEUE_MAXSIZE = 5_000
```

Modify `SessionStore.__init__`:

```python
    def __init__(self, path: Path | None = None, event_limit: int | None = None) -> None:
        self.path = path or default_session_db_path()
        self.event_limit = normalize_event_limit(event_limit)
        self._sessions: dict[str, Session] = {}
        self._last_session_id: str | None = None
        self._schema_ready = False
        self._write_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
```

Replace `_append_event` and add the writer machinery right after it:

```python
    def _append_event(self, session_id: str, event: dict[str, Any]) -> None:
        sequence = int(event.get("event_id") or 0)
        if sequence <= 0:
            return
        self._ensure_writer()
        assert self._write_queue is not None
        try:
            self._write_queue.put_nowait((session_id, dict(event)))
        except asyncio.QueueFull:
            # 事件落盘是尽力而为：队列打满时丢弃这条写入，不阻塞 agent loop。
            pass

    def _ensure_writer(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            return
        self._write_queue = asyncio.Queue(maxsize=EVENT_WRITE_QUEUE_MAXSIZE)
        self._writer_task = asyncio.get_running_loop().create_task(self._event_writer_loop())

    async def _event_writer_loop(self) -> None:
        queue = self._write_queue
        assert queue is not None
        while True:
            session_id, event = await queue.get()
            try:
                await asyncio.to_thread(self._write_event_sync, session_id, event)
            except Exception:
                # 审计/回放数据丢失不应中断 agent loop；单条写入失败不影响后续事件。
                pass
            finally:
                queue.task_done()

    def _write_event_sync(self, session_id: str, event: dict[str, Any]) -> None:
        self._ensure_schema()
        sequence = int(event.get("event_id") or 0)
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into events (session_id, sequence, payload, created_at)
                values (?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    json.dumps(event, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._prune_events(conn, session_id)

    async def flush(self) -> None:
        """等待后台写入队列中的所有事件被处理完（成功或失败）。

        测试用它来确定性地等待异步落盘完成；FastAPI 的 lifespan shutdown 钩子
        用它在进程退出前排空队列，避免丢失刚发生但还没来得及落盘的事件。
        """
        if self._write_queue is not None:
            await self._write_queue.join()
```

Confirm the old synchronous `_append_event` body (`with self._connect() as conn: conn.execute(...insert...); self._prune_events(conn, session_id)`) is fully replaced by the above, not duplicated. `_prune_events` itself is unchanged and is now called from `_write_event_sync` instead of directly from `_append_event`.

In `runtime/app/server/main.py`, extend the `lifespan` shutdown to flush pending events:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # 关闭时释放模型 provider 的 HTTP 连接池，避免长驻 daemon 连接泄漏
    await model_router.aclose()
    # 排空事件写入队列，避免刚发生但还没落盘的事件在进程退出时丢失
    await store.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd runtime && python3 -m pytest tests/test_sessions.py -v`
Expected: all PASS.

Run full suite: `cd runtime && python3 -m pytest -q`
Expected: `158 passed` (155 from Task 1 + 3 new).

- [ ] **Step 5: Commit**

```bash
git add runtime/app/sessions/store.py runtime/app/server/main.py runtime/tests/test_sessions.py
git commit -m "Move event persistence off the synchronous request-handling path

Every session.events.put() previously opened a SQLite connection and ran
an insert+prune synchronously inside the coroutine that emits the event —
blocking the asyncio event loop for the duration of the disk write on
every single tool.started/tool.output/usage.recorded/etc across the
entire daemon, not just the session that triggered it. Persistence now
goes through a bounded queue drained by one background writer task
(itself running the blocking DB call via asyncio.to_thread), making
persistence eventually-consistent and best-effort rather than a
per-event bottleneck. flush() lets tests and graceful shutdown wait
deterministically for the queue to drain.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Runtime HTTP API requires a bearer token

**Files:**
- Create: `runtime/app/server/auth.py`
- Modify: `runtime/app/server/main.py`
- Test: `runtime/tests/test_server_auth.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `runtime.app.server.auth.RUNTIME_TOKEN: str` (read from `AICODE_RUNTIME_TOKEN` env var at import time, empty string if unset); `runtime.app.server.auth.is_authorized(header_value: str) -> bool`; `runtime.app.server.auth.auth_middleware(request, call_next)` (FastAPI/Starlette HTTP middleware coroutine). `/v1/daemon/status` is exempt from the check (Task 4/5 rely on this: `ensureDaemon`'s health check must keep working without a token). Task 4/5 consume the env var name `AICODE_RUNTIME_TOKEN` as the contract between the Go daemon launcher and this middleware.

- [ ] **Step 1: Write the failing tests**

Create `runtime/tests/test_server_auth.py`:

```python
from __future__ import annotations

import httpx
import pytest

from app.server import auth
from app.server import main as server
from app.sessions.store import SessionStore


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", SessionStore(path=tmp_path / "s.sqlite"))

    def make(token: str) -> httpx.AsyncClient:
        monkeypatch.setattr(auth, "RUNTIME_TOKEN", token)
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return make


@pytest.mark.asyncio
async def test_daemon_status_is_open_even_when_token_configured(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/daemon/status")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_token_configured_allows_all_requests(client_factory) -> None:
    async with client_factory("") as client:
        response = await client.get("/v1/sessions")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_rejects_request_missing_authorization_header(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/sessions")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rejects_wrong_token(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/sessions", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_accepts_correct_token(client_factory) -> None:
    async with client_factory("secret") as client:
        response = await client.get("/v1/sessions", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200


def test_is_authorized_open_when_no_token_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "RUNTIME_TOKEN", "")
    assert auth.is_authorized("") is True
    assert auth.is_authorized("Bearer anything") is True


def test_is_authorized_rejects_malformed_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "RUNTIME_TOKEN", "secret")
    assert auth.is_authorized("secret") is False  # 缺少 "Bearer " 前缀
    assert auth.is_authorized("") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd runtime && python3 -m pytest tests/test_server_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.server.auth'`.

- [ ] **Step 3: Implement**

Create `runtime/app/server/auth.py`:

```python
from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

RUNTIME_TOKEN = os.getenv("AICODE_RUNTIME_TOKEN", "")
UNAUTHENTICATED_PATHS = {"/v1/daemon/status"}
_BEARER_PREFIX = "Bearer "


def _extract_bearer_token(header_value: str) -> str:
    if not header_value.startswith(_BEARER_PREFIX):
        return ""
    return header_value[len(_BEARER_PREFIX) :]


def is_authorized(header_value: str) -> bool:
    if not RUNTIME_TOKEN:
        return True
    provided = _extract_bearer_token(header_value)
    return hmac.compare_digest(provided, RUNTIME_TOKEN)


async def auth_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if request.url.path in UNAUTHENTICATED_PATHS:
        return await call_next(request)
    if not is_authorized(request.headers.get("authorization", "")):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)
```

In `runtime/app/server/main.py`, add the import and wire the middleware right after `app` is constructed:

```python
from app.server.auth import auth_middleware
```

(add this import in alphabetical position among the existing `from app...` imports, i.e. right after `from app.project.config import load_project_config` and before `from app.sessions.store import ...`)

```python
app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
app.middleware("http")(auth_middleware)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd runtime && python3 -m pytest tests/test_server_auth.py -v`
Expected: all 7 PASS.

Run full suite: `cd runtime && python3 -m pytest -q`
Expected: `165 passed` (158 from Task 2 + 7 new). Confirm no existing test broke — every existing test in `test_server_v2.py`/`test_server_helpers.py` calls route handler functions directly in Python (not through the ASGI app), so they never pass through `auth_middleware` and are unaffected; this run confirms that assumption holds.

- [ ] **Step 5: Commit**

```bash
git add runtime/app/server/auth.py runtime/app/server/main.py runtime/tests/test_server_auth.py
git commit -m "Require a bearer token on the Runtime HTTP API

The daemon binds 127.0.0.1 but had no request authentication: any local
process could call /approve and silently accept an edit on the user's
behalf, defeating the whole inline-diff-approval security model. When
AICODE_RUNTIME_TOKEN is set, every endpoint except /v1/daemon/status now
requires 'Authorization: Bearer <token>', compared with hmac.compare_digest
to avoid timing leaks. Unset (the manual 'cd runtime && uvicorn ...' dev
flow) keeps today's open-by-default behavior — token issuance and
transport are wired in Task 4/5.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: CLI daemon generates and persists the auth token

**Files:**
- Modify: `cli/internal/daemon/daemon.go`
- Test: `cli/internal/daemon/daemon_test.go` (new file — package currently has none)

**Interfaces:**
- Consumes: `config.Home() (string, error)` (existing), `cfg.RuntimeEnv() []string` (existing).
- Produces: `daemon.Token() string` — reads the persisted token file, returns `""` on any error (missing file, unreadable, etc. — a missing token is treated identically to "no auth configured" from the CLI's read side; the server enforces the real fail-closed behavior from Task 3 if it expects a token and doesn't get a matching one). Task 5 consumes `Token()`.

- [ ] **Step 1: Write the failing tests**

Create `cli/internal/daemon/daemon_test.go`:

```go
package daemon

import (
	"os"
	"path/filepath"
	"testing"
)

func TestGenerateTokenProducesDistinctHexStrings(t *testing.T) {
	first, err := generateToken()
	if err != nil {
		t.Fatalf("generateToken failed: %v", err)
	}
	second, err := generateToken()
	if err != nil {
		t.Fatalf("generateToken failed: %v", err)
	}
	if len(first) != 64 {
		t.Fatalf("expected 64 hex chars (32 bytes), got %d: %q", len(first), first)
	}
	if first == second {
		t.Fatalf("expected two calls to generateToken to differ, both were %q", first)
	}
}

func TestTokenReadsPersistedFile(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)

	if err := os.WriteFile(tokenPath(home), []byte("abc123\n"), 0o600); err != nil {
		t.Fatalf("failed to seed token file: %v", err)
	}

	got := Token()
	if got != "abc123" {
		t.Fatalf("expected Token() to return %q, got %q", "abc123", got)
	}
}

func TestTokenReturnsEmptyWhenFileMissing(t *testing.T) {
	home := t.TempDir()
	t.Setenv("AICODE_HOME", home)

	got := Token()
	if got != "" {
		t.Fatalf("expected empty token when file is missing, got %q", got)
	}
}

func TestTokenPath(t *testing.T) {
	got := tokenPath(filepath.Join("home", ".aicode"))
	want := filepath.Join("home", ".aicode", "runtime.token")
	if got != want {
		t.Fatalf("expected %q, got %q", want, got)
	}
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `go test ./cli/internal/daemon/... -v`
Expected: FAIL — build error (`undefined: generateToken`, `undefined: tokenPath`, `undefined: Token`).

- [ ] **Step 3: Implement**

In `cli/internal/daemon/daemon.go`, add imports:

```go
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

	"github.com/aicode-dev/aicode/cli/internal/config"
)
```

Add near the top, after the imports:

```go
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
```

Modify `Start()` to generate and persist a token, and pass it to the Runtime subprocess's environment:

```go
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
```

Modify `Stop()` to remove the token file alongside the pid file:

```go
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `go test ./cli/internal/daemon/... -v`
Expected: all 4 new tests PASS.

Run full CLI suite: `go test ./cli/...`
Expected: all packages `ok`.

- [ ] **Step 5: Commit**

```bash
git add cli/internal/daemon/daemon.go cli/internal/daemon/daemon_test.go
git commit -m "Generate and persist a Runtime auth token on daemon start

daemon.Start() now generates a random 32-byte token, writes it to
~/.aicode/runtime.token (0600), and passes it to the Runtime subprocess
via AICODE_RUNTIME_TOKEN so Task 3's middleware enforces it. Stop()
removes the token file alongside the pid file. Token() lets callers read
the currently-persisted token; Task 5 wires it into the HTTP client.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: CLI HTTP client sends the auth token

**Files:**
- Modify: `cli/internal/client/client.go`
- Modify: `cli/internal/client/client_test.go`
- Modify: `cli/main.go`

**Interfaces:**
- Consumes: `daemon.Token() string` (Task 4).
- Produces: `client.New(baseURL string, token string) Client` (signature change — was `New(baseURL string) Client`). All 4 production call sites and all 5 test call sites are updated in this task. No later task depends on this beyond the compiled binary working end-to-end.

- [ ] **Step 1: Write the failing test, update existing call sites to the new signature**

In `cli/internal/client/client_test.go`, update every existing `New(...)` call to pass an explicit empty token (these tests don't exercise auth, so `""` preserves current behavior — no header sent):

Find (4 occurrences): `New("http://runtime.test")` → replace with `New("http://runtime.test", "")`.

Find: `New(server.URL)` (1 occurrence, inside `TestApproveSendsAcceptAll`) → replace with `New(server.URL, "")`.

Add a new test after `TestApproveSendsAcceptAll`:

```go
func TestRequestsIncludeAuthorizationHeaderWhenTokenSet(t *testing.T) {
	var gotHeader string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHeader = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()

	c := New(server.URL, "secret-token")
	if _, err := c.GetJSON(context.Background(), "/v1/sessions"); err != nil {
		t.Fatalf("GetJSON failed: %v", err)
	}
	if gotHeader != "Bearer secret-token" {
		t.Fatalf("expected Authorization header %q, got %q", "Bearer secret-token", gotHeader)
	}
}

func TestRequestsOmitAuthorizationHeaderWhenNoToken(t *testing.T) {
	var gotHeader string
	sawRequest := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawRequest = true
		gotHeader = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()

	c := New(server.URL, "")
	if _, err := c.GetJSON(context.Background(), "/v1/sessions"); err != nil {
		t.Fatalf("GetJSON failed: %v", err)
	}
	if !sawRequest {
		t.Fatalf("expected server to receive a request")
	}
	if gotHeader != "" {
		t.Fatalf("expected no Authorization header, got %q", gotHeader)
	}
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `go test ./cli/internal/client/... -v`
Expected: build FAILS — `New` is called with 2 arguments but only accepts 1 (compile error across the whole `_test.go` file once the new calls are added while `New`'s signature is still single-arg).

- [ ] **Step 3: Implement**

In `cli/internal/client/client.go`, modify the `Client` struct and `New`:

```go
type Client struct {
	baseURL string
	http    *http.Client
	token   string
}

func New(baseURL string, token string) Client {
	return Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		http: &http.Client{
			Timeout: 0,
		},
		token: token,
	}
}

func (c Client) setAuthHeader(req *http.Request) {
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
}
```

Call `c.setAuthHeader(req)` right after each `http.NewRequestWithContext` call succeeds, in all three places that build a request:

In `GetJSON`:
```go
func (c Client) GetJSON(ctx context.Context, path string) (any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return nil, err
	}
	c.setAuthHeader(req)

	resp, err := c.http.Do(req)
	...
```

In `postJSON`:
```go
func (c Client) postJSON(ctx context.Context, path string, payload any, out any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	c.setAuthHeader(req)

	httpClient := c.http
	...
```

In `newStreamRequest`:
```go
func (c Client) newStreamRequest(ctx context.Context, sessionID string, runID string, after int64) (*http.Request, error) {
	streamURL := c.baseURL + "/v1/sessions/" + url.PathEscape(sessionID) + "/events"
	params := url.Values{}
	if after > 0 {
		params.Set("after", strconv.FormatInt(after, 10))
	}
	if runID != "" {
		params.Set("run_id", runID)
	}
	if encoded := params.Encode(); encoded != "" {
		streamURL += "?" + encoded
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, streamURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "text/event-stream")
	if after > 0 {
		req.Header.Set("Last-Event-ID", strconv.FormatInt(after, 10))
	}
	c.setAuthHeader(req)
	return req, nil
}
```

In `cli/main.go`, update all 4 `client.New(cfg.Runtime.URL)` call sites to `client.New(cfg.Runtime.URL, daemon.Token())` (the `"github.com/aicode-dev/aicode/cli/internal/daemon"` package is already imported in this file).

- [ ] **Step 4: Run tests to verify they pass**

Run: `go test ./cli/internal/client/... -v`
Expected: all tests PASS including the 2 new ones.

Run full CLI suite: `go test ./cli/...`
Expected: all packages `ok` (this also confirms `cli/main.go`'s 4 updated call sites compile).

Run full Python suite too (nothing here touches Python, this is a cheap regression check): `cd runtime && python3 -m pytest -q`
Expected: `165 passed` (unchanged from Task 3).

- [ ] **Step 5: Commit**

```bash
git add cli/internal/client/client.go cli/internal/client/client_test.go cli/main.go
git commit -m "Send the Runtime auth token as a bearer header on every CLI request

client.New now takes a token and attaches 'Authorization: Bearer <token>'
to every GET/POST/SSE request when non-empty, completing the auth loop
started in Task 3/4: the CLI reads daemon.Token() (the file Start()
just wrote) and sends it on every call, so the daemon's approve/reject/
messages endpoints are no longer reachable by another local process that
doesn't know the token.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: In-memory session cache evicts idle sessions, never running or approval-pending ones

**Files:**
- Modify: `runtime/app/sessions/store.py`
- Test: `runtime/tests/test_sessions.py`

**Interfaces:**
- Consumes: Task 2's `SessionStore` shape (this task adds a new independent piece of state, `_cache_limit`, and doesn't touch the write-queue internals).
- Produces: `SessionStore.__init__(path, event_limit, cache_limit=None)` gains an optional `cache_limit` kwarg (default from `AICODE_SESSION_CACHE_LIMIT` env var, falling back to `DEFAULT_SESSION_CACHE_LIMIT = 200`); a new module function `normalize_cache_limit(value=None) -> int` mirroring the existing `normalize_event_limit`. No later task depends on internals beyond this being self-contained.

- [ ] **Step 1: Write the failing tests**

Add to `runtime/tests/test_sessions.py`, at the end of the file (after `test_normalize_event_limit_uses_minimum_one`), and update the import line at the top:

Change:
```python
from app.sessions.store import SessionEvents, SessionStore, normalize_event_limit
```
to:
```python
from app.sessions.store import SessionEvents, SessionStore, normalize_cache_limit, normalize_event_limit
```

Append:

```python
def test_idle_sessions_are_evicted_beyond_cache_limit(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=2)
    first = store.create(workspace="/repo1", language="zh-CN")
    second = store.create(workspace="/repo2", language="zh-CN")
    third = store.create(workspace="/repo3", language="zh-CN")

    assert len(store._sessions) == 2
    assert first.session_id not in store._sessions
    assert second.session_id in store._sessions
    assert third.session_id in store._sessions


def test_evicted_session_is_still_reachable_via_get(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
    first = store.create(workspace="/repo1", language="zh-CN")
    store.create(workspace="/repo2", language="zh-CN")

    assert first.session_id not in store._sessions

    reloaded = store.get(first.session_id)

    assert reloaded is not None
    assert reloaded.session_id == first.session_id
    assert reloaded.workspace == "/repo1"


def test_get_refreshes_recency_and_protects_from_eviction(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=2)
    first = store.create(workspace="/repo1", language="zh-CN")
    store.create(workspace="/repo2", language="zh-CN")

    # 触碰 first，使其成为最近使用
    store.get(first.session_id)

    store.create(workspace="/repo3", language="zh-CN")

    # first 因为刚被访问过，不应被驱逐；repo2 应被驱逐
    assert first.session_id in store._sessions


def test_session_with_pending_approval_is_never_evicted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
    pending = store.create(workspace="/repo1", language="zh-CN")
    pending.create_approval("edit", {"path": "a.py"})

    idle = store.create(workspace="/repo2", language="zh-CN")

    # cache_limit=1 且 pending 有未决 approval，不可驱逐；idle 是唯一可驱逐的，被驱逐出去
    assert pending.session_id in store._sessions
    assert idle.session_id not in store._sessions


def test_session_with_active_agent_runner_is_never_evicted(tmp_path: Path) -> None:
    import asyncio

    async def _never_finishes() -> None:
        await asyncio.sleep(3600)

    async def run() -> None:
        store = SessionStore(tmp_path / "sessions.sqlite", cache_limit=1)
        running = store.create(workspace="/repo1", language="zh-CN")
        running.agent_runner_task = asyncio.create_task(_never_finishes())

        idle = store.create(workspace="/repo2", language="zh-CN")

        assert running.session_id in store._sessions
        assert idle.session_id not in store._sessions

        running.agent_runner_task.cancel()
        try:
            await running.agent_runner_task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_normalize_cache_limit_uses_minimum_one(monkeypatch: pytest.MonkeyPatch) -> None:
    assert normalize_cache_limit(0) == 1

    monkeypatch.setenv("AICODE_SESSION_CACHE_LIMIT", "bad")
    assert normalize_cache_limit() == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd runtime && python3 -m pytest tests/test_sessions.py -v`
Expected: FAIL — `TypeError: SessionStore.__init__() got an unexpected keyword argument 'cache_limit'` on every new test, and `ImportError: cannot import name 'normalize_cache_limit'` at collection time for the whole file (since the top-level import line was changed to include it).

- [ ] **Step 3: Implement**

In `runtime/app/sessions/store.py`, add the constant near the other defaults:

```python
DEFAULT_SESSION_EVENT_LIMIT = 2_000
MAX_TRANSIENT_RETAINED_EVENTS = 200
EVENT_WRITE_QUEUE_MAXSIZE = 5_000
DEFAULT_SESSION_CACHE_LIMIT = 200
```

Modify `SessionStore.__init__`:

```python
    def __init__(self, path: Path | None = None, event_limit: int | None = None, cache_limit: int | None = None) -> None:
        self.path = path or default_session_db_path()
        self.event_limit = normalize_event_limit(event_limit)
        self._cache_limit = normalize_cache_limit(cache_limit)
        self._sessions: dict[str, Session] = {}
        self._last_session_id: str | None = None
        self._schema_ready = False
        self._write_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
```

Add `_touch`, `_evict_if_needed`, `_is_evictable` methods (place them right after `__init__`, before `create`):

```python
    def _touch(self, session: Session) -> None:
        """把 session 标记为最近使用，并在超出缓存上限时驱逐最久未用的可驱逐 session。

        驱逐只丢弃内存中的 Session 对象（SessionEvents 缓冲、待决 approval 的
        asyncio.Event、agent_queue）；SQLite 里的 session/message/event 行不受影响。
        再次 get() 会从数据库重新构建一个新的 Session 对象，行为等同于 daemon 重启后
        首次访问这个 session——已有的读路径本就支持这种情况。
        """
        self._sessions.pop(session.session_id, None)
        self._sessions[session.session_id] = session
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if len(self._sessions) <= self._cache_limit:
            return
        for session_id in list(self._sessions.keys()):
            if len(self._sessions) <= self._cache_limit:
                return
            candidate = self._sessions[session_id]
            if self._is_evictable(candidate):
                del self._sessions[session_id]

    def _is_evictable(self, session: Session) -> bool:
        if session.agent_runner_active():
            return False
        if any(approval.accepted is None for approval in session.approvals.values()):
            return False
        return True
```

Replace every direct `self._sessions[session.session_id] = session` assignment with a call to `self._touch(session)`, and make the cache-hit branch of `get()` also touch. The four sites:

`create`:
```python
    def create(self, workspace: str, language: str) -> Session:
        self._ensure_schema()
        session = Session(
            session_id=f"sess_{uuid4().hex[:12]}",
            workspace=workspace,
            language=language,
        )
        self._attach_events(session)
        session.updated_at = session.created_at
        self._touch(session)
        self._last_session_id = session.session_id
        self._insert_session(session)
        return session
```

`get`:
```python
    def get(self, session_id: str) -> Session | None:
        self._ensure_schema()
        cached = self._sessions.get(session_id)
        if cached is not None:
            self._touch(cached)
            return cached

        with self._connect() as conn:
            row = conn.execute(
                "select session_id, workspace, language, created_at, updated_at from sessions where session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages = self._load_messages(conn, session.session_id)
            self._attach_events(session, self._load_events(conn, session.session_id))
            self._touch(session)
            return session
```

`list` (inside the loop):
```python
    def list(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "select rowid, session_id, workspace, language, created_at, updated_at from sessions order by updated_at desc, rowid desc"
            ).fetchall()
            sessions: list[dict[str, Any]] = []
            for row in rows:
                session = self._sessions.get(row["session_id"])
                if session is None:
                    session = self._session_from_row(row)
                    self._attach_events(session, self._load_events(conn, session.session_id))
                session.messages = self._load_messages(conn, session.session_id)
                self._touch(session)
                sessions.append(session.to_dict())
            return sessions
```

`last`:
```python
    def last(self) -> Session | None:
        self._ensure_schema()
        if self._last_session_id:
            cached = self.get(self._last_session_id)
            if cached is not None:
                return cached

        with self._connect() as conn:
            row = conn.execute(
                "select rowid, session_id, workspace, language, created_at, updated_at from sessions order by updated_at desc, rowid desc limit 1"
            ).fetchone()
            if row is None:
                return None
            session = self._session_from_row(row)
            session.messages = self._load_messages(conn, session.session_id)
            self._attach_events(session, self._load_events(conn, session.session_id))
            self._touch(session)
            self._last_session_id = session.session_id
            return session
```

Add `normalize_cache_limit` at the bottom of the file, right after `normalize_event_limit`:

```python
def normalize_cache_limit(value: int | None = None) -> int:
    if value is None:
        raw = os.getenv("AICODE_SESSION_CACHE_LIMIT")
        if not raw:
            return DEFAULT_SESSION_CACHE_LIMIT
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_SESSION_CACHE_LIMIT
    return max(1, int(value))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd runtime && python3 -m pytest tests/test_sessions.py -v`
Expected: all PASS, including all pre-existing tests (default `cache_limit=200` is far above anything any existing test creates, so `_evict_if_needed` never trims in those tests — confirm by reading test output for zero failures, not just the new ones).

Run full suite: `cd runtime && python3 -m pytest -q`
Expected: `171 passed` (165 from Task 3 + 6 new).

- [ ] **Step 5: Commit**

```bash
git add runtime/app/sessions/store.py runtime/tests/test_sessions.py
git commit -m "Evict idle sessions from the in-memory cache under a bounded limit

SessionStore._sessions grew without bound for the lifetime of the daemon
process — every session ever touched stayed cached forever. Sessions are
now LRU-evicted once the cache exceeds AICODE_SESSION_CACHE_LIMIT
(default 200), but a session with an active agent run or an unresolved
approval is never evicted (evicting it would drop the asyncio.Event a
concurrent approve/reject call is waiting on, hanging that request
forever with no way to resolve it). Evicting only drops the in-memory
object; SQLite rows are untouched and get() transparently reconstructs
an evicted session on next access, identical to how it already handles a
session touched for the first time after a daemon restart.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing (docs-only).

- [ ] **Step 1: Update the environment variables list**

In `README.md`, find the "常用环境变量" code block (currently ending with `AICODE_MODEL_PRICES_JSON`) and add `AICODE_SESSION_CACHE_LIMIT` next to the existing `AICODE_SESSION_EVENT_LIMIT` line:

Find:
```
export AICODE_SESSION_EVENT_LIMIT="2000"
export AICODE_MODEL_PRICES_JSON='{"openai_compatible/gpt-5":{"input_per_1m":1.25,"output_per_1m":10}}'
```

Replace with:
```
export AICODE_SESSION_EVENT_LIMIT="2000"
export AICODE_SESSION_CACHE_LIMIT="200"
export AICODE_MODEL_PRICES_JSON='{"openai_compatible/gpt-5":{"input_per_1m":1.25,"output_per_1m":10}}'
```

- [ ] **Step 2: Document the daemon auth token**

In `README.md`, find the "Session 持久化" section (which documents `AICODE_SESSION_EVENT_LIMIT`) and add a new subsection right after it, before "## 配置":

```markdown
## Runtime 认证

`aicode daemon start` 会在 `~/.aicode/runtime.token`（0600 权限）生成一个随机 token，并传给 Runtime 子进程；CLI 之后的每次请求都会带上 `Authorization: Bearer <token>`。`aicode daemon stop` 会清理这个 token 文件。这道认证防止同一台机器上的其它进程未经确认就调用 `/approve` 之类的接口。

手动启动 Runtime（`cd runtime && python3 -m uvicorn ...`，不经过 `aicode daemon start`）时不会设置 `AICODE_RUNTIME_TOKEN`，此时 API 保持不认证，方便本地调试；如果需要给手动启动的 Runtime 也加上认证，自行 `export AICODE_RUNTIME_TOKEN=...` 后启动即可，但对应的 CLI 请求也需要一致的 token 才能通过。
```

- [ ] **Step 3: Verify doc claims match code**

Run: `grep -n "AICODE_RUNTIME_TOKEN\|AICODE_SESSION_CACHE_LIMIT" README.md runtime/app/server/auth.py runtime/app/sessions/store.py cli/internal/daemon/daemon.go`
Expected: `AICODE_RUNTIME_TOKEN` appears in `README.md` (2x), `runtime/app/server/auth.py` (env var read), and `cli/internal/daemon/daemon.go` (env var set on `cmd.Env`); `AICODE_SESSION_CACHE_LIMIT` appears in `README.md` and `runtime/app/sessions/store.py`. Confirms the doc isn't describing anything that doesn't exist in code.

Run the full test suites one last time to confirm the doc-only change didn't accidentally touch anything: `cd runtime && python3 -m pytest -q` (expect `171 passed`) and `go test ./cli/...` (expect all `ok`).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document AICODE_RUNTIME_TOKEN and AICODE_SESSION_CACHE_LIMIT

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** all three items from the daemon-stability review are covered — `assistant.delta` transient handling + async event write-behind (Task 1, 2), API token authentication end-to-end (Task 3, 4, 5), session in-memory eviction with the running/pending-approval guard (Task 6). Docs task (7) keeps `README.md` truthful, matching the project's established pattern of a dedicated final docs task.

**Placeholder scan:** no TBD/TODO; every step has runnable code and exact commands; no "similar to Task N" — Task 6's four `_sessions[...] = session` call sites are each written out in full rather than referenced.

**Type consistency:** `SessionStore.__init__(path, event_limit=None, cache_limit=None)` (Task 6) is additive to Task 2's shape (no new positional args, so `SessionStore(db_path)` and `SessionStore(db_path, event_limit=2)` call sites used throughout the existing test suite keep working unchanged). `client.New(baseURL, token)` (Task 5) is the one true breaking signature change in this plan — Task 5's own step 1 updates every call site in the same task, and no other task calls `client.New`, so nothing is left inconsistent. `daemon.Token() string` (Task 4) and `store.flush() -> None` / `SessionStore._write_event_sync(session_id, event) -> None` (Task 2) names are used identically by every task/test that references them.
