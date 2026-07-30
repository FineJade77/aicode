from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# How a tool call reaches execution.
#
# "none"  — runs without asking (read-only inspection).
# "gate"  — the policy engine decides allow/ask/deny.
# "diff"  — the Agent Loop presents a diff and applies it only once approved.
ApprovalMode = Literal["none", "gate", "diff"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Everything the Runtime needs to know about a tool besides how to run it.

    Lives in core rather than next to the tool implementations because three
    layers read it and none of them may import the others: the model sees
    `input_schema`, the policy engine reads `read_only`, and the Agent Loop reads
    `approval`. Keeping one declaration is what stops read-only-ness from being
    maintained separately in the registry and in the policy engine, which is
    exactly the drift this replaces.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    approval: ApprovalMode = "gate"
    # Modes that must not even see this tool, so the model cannot attempt a call
    # the policy layer would then have to reject.
    hidden_in_modes: frozenset[str] = field(default_factory=frozenset)

    def to_schema(self) -> dict[str, Any]:
        """The wire form handed to a model provider."""
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def visible_in(self, mode: str) -> bool:
        return mode not in self.hidden_in_modes
