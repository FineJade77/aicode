from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.provider import ModelProvider, ModelProviderUnavailable, ModelRequest, ModelResponse, StubProvider


@dataclass(slots=True)
class ModelRouter:
    primary: ModelProvider
    fallback: ModelProvider
    settings: Settings

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRouter":
        return cls(
            primary=OpenAICompatibleProvider(settings.openai_compatible),
            fallback=StubProvider(),
            settings=settings,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        routed = ModelRequest(
            purpose=request.purpose,
            messages=request.messages,
            model=request.model or self.model_for_purpose(request.purpose),
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )

        try:
            return await self.primary.complete(routed)
        except ModelProviderUnavailable:
            return await self.fallback.complete(
                ModelRequest(
                    purpose=routed.purpose,
                    messages=routed.messages,
                    model="stub",
                    temperature=routed.temperature,
                    max_tokens=routed.max_tokens,
                )
            )

    def model_for_purpose(self, purpose: str) -> str:
        if purpose == "planner":
            return self.settings.models.planner
        if purpose == "coder":
            return self.settings.models.coder
        if purpose == "reviewer":
            return self.settings.models.reviewer
        if purpose == "summarizer":
            return self.settings.models.summarizer
        return self.settings.models.default
