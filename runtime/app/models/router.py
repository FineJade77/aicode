from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config.settings import Settings
from app.models.anthropic import AnthropicProvider
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.provider import (
    CompletionRequest,
    CompletionResult,
    ModelCapability,
    ProviderCapabilityError,
    ProviderNotConfigured,
    StreamingModelProvider,
    ToolCallRequest,
    Usage,
)
from app.usage.pricing import estimate_cost, model_prices_data


@dataclass(slots=True)
class ModelRouter:
    primary: StreamingModelProvider
    settings: Settings

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelRouter:
        if settings.provider.type == "anthropic":
            primary: StreamingModelProvider = AnthropicProvider(settings.anthropic)
        else:
            primary = OpenAICompatibleProvider(settings.openai_compatible)
        return cls(primary=primary, settings=settings)

    def model_for_purpose(self, purpose: str) -> str:
        if purpose == "reviewer":
            return self.settings.models.reviewer
        if purpose == "summarizer":
            return self.settings.models.summarizer
        return self.settings.models.main

    def capability_for_purpose(self, purpose: str) -> ModelCapability:
        return self.capability_for_model(self.model_for_purpose(purpose))

    def capability_for_model(self, model: str) -> ModelCapability:
        provider = str(getattr(self.primary, "provider_name", self.primary.__class__.__name__))
        provider_key = f"{provider}:{model}"
        contexts = self.settings.context.model_context_windows
        outputs = self.settings.context.model_max_output_tokens
        local_profile = self.settings.openai_compatible if provider == "openai_compatible" else None
        default_context = (
            local_profile.context_window if local_profile is not None else self.settings.context.default_context_window
        )
        default_output = (
            local_profile.max_output_tokens
            if local_profile is not None
            else self.settings.context.default_max_output_tokens
        )
        context_window = contexts.get(provider_key, contexts.get(model, default_context))
        max_output_tokens = outputs.get(provider_key, outputs.get(model, default_output))
        source = "configured" if provider_key in contexts or model in contexts else ("profile" if local_profile else "default")
        return ModelCapability(
            provider=provider,
            model=model,
            context_window=context_window,
            max_output_tokens=min(max_output_tokens, max(1, context_window - 1)),
            source=source,
            tool_calling=local_profile.tool_calling if local_profile is not None else True,
            streaming=local_profile.streaming if local_profile is not None else True,
            tokenizer=local_profile.tokenizer if local_profile is not None else "chars",
            chars_per_token=(
                local_profile.chars_per_token if local_profile is not None else self.settings.context.chars_per_token
            ),
        )

    async def aclose(self) -> None:
        aclose = getattr(self.primary, "aclose", None)
        if callable(aclose):
            await aclose()

    async def stream_complete(
        self,
        *,
        purpose: str,
        model: str | None = None,
        system: str,
        messages: list[dict],
        tools: list[dict] | tuple = (),
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> CompletionResult:
        is_configured = getattr(self.primary, "is_configured", None)
        if callable(is_configured) and not is_configured():
            raise ProviderNotConfigured("The model provider is not configured. Set an API key and retry.")
        selected_model = model or self.model_for_purpose(purpose)
        capability = self.capability_for_model(selected_model)
        if not capability.streaming:
            raise ProviderCapabilityError(
                f"provider profile does not support streaming for {capability.provider}:{capability.model}"
            )
        if tools and not capability.tool_calling:
            raise ProviderCapabilityError(
                f"provider profile does not support native tools for {capability.provider}:{capability.model}; "
                "text JSON fallback is disabled"
            )
        request = CompletionRequest(
            purpose=purpose,
            system=system,
            messages=messages,
            tools=list(tools),
            model=selected_model,
            temperature=temperature,
            max_tokens=min(max_tokens, capability.max_output_tokens),
        )
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        usage = Usage()
        model_name = request.model
        async for event in self.primary.stream_complete(request):
            if event.type == "text_delta":
                text_parts.append(event.text)
                if on_text_delta is not None:
                    await on_text_delta(event.text)
            elif event.type == "tool_call" and event.tool_call is not None:
                tool_calls.append(event.tool_call)
            elif event.type == "done":
                usage = event.usage or usage
                model_name = event.model or model_name
        provider_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            model=model_name,
            provider=provider_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost=estimate_cost(
                provider=provider_name,
                model=model_name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                prices=self.settings.pricing.model_prices,
            ),
        )

    async def probe(self, *, model: str | None = None, tools: bool = True) -> dict:
        selected_model = model or self.settings.models.main
        probe = getattr(self.primary, "probe", None)
        if not callable(probe):
            result: dict = {
                "schema_version": 1,
                "status": "error",
                "model": selected_model,
                "discovered_models": [],
                "checks": [
                    {
                        "name": "provider",
                        "status": "fail",
                        "code": "probe_unsupported",
                        "summary": f"provider {self.settings.provider.type!r} does not implement the models probe",
                    }
                ],
                "latency_ms": 0,
            }
        else:
            result = await probe(selected_model, tools=tools)
        result["profile"] = self.profile_status(model=selected_model)
        return result

    def profile_status(self, *, model: str | None = None) -> dict:
        selected_model = model or self.settings.models.main
        capability = self.capability_for_model(selected_model)
        if self.settings.provider.type == "anthropic":
            return {
                "schema_version": 1,
                "name": "anthropic",
                "provider": "anthropic",
                "base_url": self.settings.anthropic.base_url,
                "auth_mode": "required",
                "api_key_env": self.settings.anthropic.api_key_env,
                "model": selected_model,
                "context_window": capability.context_window,
                "max_output_tokens": capability.max_output_tokens,
                "tool_calling": capability.tool_calling,
                "streaming": capability.streaming,
                "tokenizer": capability.tokenizer,
                "chars_per_token": capability.chars_per_token,
            }
        profile = self.settings.openai_compatible
        return {
            "schema_version": profile.profile_schema_version,
            "name": profile.profile,
            "provider": self.settings.provider.type,
            "base_url": profile.base_url,
            "auth_mode": profile.auth_mode,
            "api_key_env": profile.api_key_env,
            "model": selected_model,
            "context_window": capability.context_window,
            "max_output_tokens": capability.max_output_tokens,
            "tool_calling": capability.tool_calling,
            "streaming": capability.streaming,
            "tokenizer": capability.tokenizer,
            "chars_per_token": capability.chars_per_token,
        }

    def route_status(self) -> dict:
        primary_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
        is_configured = getattr(self.primary, "is_configured", None)
        primary_configured = bool(is_configured()) if callable(is_configured) else True
        return {
            "provider": {
                "primary": primary_name,
                "primary_configured": primary_configured,
                "type": self.settings.provider.type,
            },
            "routes": {
                "main": self.settings.models.main,
                "reviewer": self.settings.models.reviewer,
                "summarizer": self.settings.models.summarizer,
            },
            "capabilities": {
                purpose: {
                    "provider": capability.provider,
                    "model": capability.model,
                    "context_window": capability.context_window,
                    "max_output_tokens": capability.max_output_tokens,
                    "source": capability.source,
                    "tool_calling": capability.tool_calling,
                    "streaming": capability.streaming,
                    "tokenizer": capability.tokenizer,
                    "chars_per_token": capability.chars_per_token,
                }
                for purpose in ("main", "reviewer", "summarizer")
                for capability in (self.capability_for_purpose(purpose),)
            },
            "openai_compatible": {
                "profile": self.settings.openai_compatible.profile,
                "profile_schema_version": self.settings.openai_compatible.profile_schema_version,
                "base_url": self.settings.openai_compatible.base_url,
                "api_key_env": self.settings.openai_compatible.api_key_env,
                "auth_mode": self.settings.openai_compatible.auth_mode,
                "timeout_seconds": self.settings.openai_compatible.timeout_seconds,
                "tool_calling": self.settings.openai_compatible.tool_calling,
                "streaming": self.settings.openai_compatible.streaming,
            },
            "profile": self.profile_status(),
            "pricing": {
                "currency": self.settings.pricing.currency,
                "unit": "per_1m_tokens",
                "models": model_prices_data(self.settings.pricing.model_prices),
            },
        }
