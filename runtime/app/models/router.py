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

    def route_status(self) -> dict:
        primary_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
        fallback_name = getattr(self.fallback, "provider_name", self.fallback.__class__.__name__)
        is_configured = getattr(self.primary, "is_configured", None)
        primary_configured = bool(is_configured()) if callable(is_configured) else True
        return {
            "provider": {
                "primary": primary_name,
                "primary_configured": primary_configured,
                "fallback": fallback_name,
            },
            "routes": {
                "default": self.settings.models.default,
                "planner": self.settings.models.planner,
                "coder": self.settings.models.coder,
                "reviewer": self.settings.models.reviewer,
                "summarizer": self.settings.models.summarizer,
            },
            "openai_compatible": {
                "base_url": self.settings.openai_compatible.base_url,
                "api_key_env": self.settings.openai_compatible.api_key_env,
                "timeout_seconds": self.settings.openai_compatible.timeout_seconds,
            },
        }
