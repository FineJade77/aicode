from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping

from app.core.session import AgentSession


APPLICATION_CONTRACT_VERSION = "2.0"

RunAdmissionStatus = Literal["accepted", "queued"]
RunControlStatus = Literal["cancelled", "idle"]
CompactionStatus = Literal["compacted", "unchanged"]


def application_contract_descriptor() -> dict[str, Any]:
    return {
        "version": APPLICATION_CONTRACT_VERSION,
        "schema": "v2",
        "types": {
            "session": "SessionSnapshot",
            "turn": "TurnRequest",
            "run": "RunReceipt",
            "run_control": "RunControl",
        },
    }


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Transport-independent input for one session turn."""

    message: str
    mode: str
    workspace: str
    model: str | None = None

    def bind(self, *, workspace: str) -> TurnRequest:
        return replace(self, workspace=workspace)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": self.message,
            "mode": self.mode,
            "workspace": self.workspace,
        }
        if self.model is not None:
            payload["model"] = self.model
        return payload


@dataclass(frozen=True, slots=True)
class RunReceipt:
    status: RunAdmissionStatus
    run_id: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "run_id": self.run_id}


@dataclass(frozen=True, slots=True)
class RunControl:
    status: RunControlStatus
    run_id: str | None
    queued: int

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "run_id": self.run_id, "queued": self.queued}


@dataclass(frozen=True, slots=True)
class SteerReceipt:
    run_id: str
    pending: int
    status: Literal["queued"] = "queued"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "run_id": self.run_id, "pending": self.pending}


@dataclass(frozen=True, slots=True)
class CompactionReceipt:
    status: CompactionStatus
    compaction: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "compaction": None if self.compaction is None else dict(self.compaction),
        }


@dataclass(frozen=True, slots=True)
class AgentRunState:
    running: bool
    queued: int
    pending_steers: int
    current_run_id: str | None
    stage: str | None
    started_at: str | None
    last_progress_at: str | None
    elapsed_seconds: int | None
    stalled_seconds: int | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AgentRunState:
        return cls(
            running=bool(value.get("running", False)),
            queued=_int_value(value.get("queued"), 0),
            pending_steers=_int_value(value.get("pending_steers"), 0),
            current_run_id=_optional_string(value.get("current_run_id")),
            stage=_optional_string(value.get("stage")),
            started_at=_optional_string(value.get("started_at")),
            last_progress_at=_optional_string(value.get("last_progress_at")),
            elapsed_seconds=_optional_int(value.get("elapsed_seconds")),
            stalled_seconds=_optional_int(value.get("stalled_seconds")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "queued": self.queued,
            "pending_steers": self.pending_steers,
            "current_run_id": self.current_run_id,
            "stage": self.stage,
            "started_at": self.started_at,
            "last_progress_at": self.last_progress_at,
            "elapsed_seconds": self.elapsed_seconds,
            "stalled_seconds": self.stalled_seconds,
        }


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Stable read model exposed by Application Runtime."""

    session_id: str
    workspace: str
    created_at: str
    updated_at: str
    messages: tuple[dict[str, Any], ...]
    approvals: tuple[dict[str, Any], ...]
    agent: AgentRunState

    @classmethod
    def from_session(cls, session: AgentSession) -> SessionSnapshot:
        return cls.from_mapping(session.to_dict())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SessionSnapshot:
        messages = value.get("messages")
        approvals = value.get("approvals")
        agent = value.get("agent")
        return cls(
            session_id=str(value.get("session_id") or ""),
            workspace=str(value.get("workspace") or ""),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            messages=tuple(dict(item) for item in messages or () if isinstance(item, Mapping)),
            approvals=tuple(dict(item) for item in approvals or () if isinstance(item, Mapping)),
            agent=AgentRunState.from_mapping(agent if isinstance(agent, Mapping) else {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [dict(item) for item in self.messages],
            "approvals": [dict(item) for item in self.approvals],
            "agent": self.agent.to_dict(),
        }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _int_value(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
