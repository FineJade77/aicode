from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ModelRequest:
    purpose: str
    messages: list[dict[str, str]]
    model: str | None = None


@dataclass(slots=True)
class ModelResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0


class ModelProvider:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError


class StubProvider(ModelProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text="Runtime 骨架已连接。模型 provider 后续接入 OpenAI-compatible API。",
            model=request.model or "stub",
            input_tokens=sum(len(message.get("content", "")) for message in request.messages),
            output_tokens=18,
            estimated_cost=0.0,
        )
