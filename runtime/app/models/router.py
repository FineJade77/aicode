from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from app.config import Settings
from app.models.anthropic import AnthropicProvider
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.provider import (
    CompletionRequest,
    CompletionResult,
    ContextOverflowError,
    ModelCapability,
    ProviderCapabilityError,
    ProviderError,
    ProviderNotConfigured,
    StreamingModelProvider,
    ToolCallRequest,
    Usage,
)
from app.usage.pricing import estimate_cost, model_prices_data, price_for

# Probe results are "ok" or "error"; ordered worst-first so aggregation is a
# min over this list rather than a chain of comparisons.
_STATUS_ORDER = ("error", "warn", "ok")


def _worst_status(statuses) -> str:
    seen = {status for status in statuses}
    for status in _STATUS_ORDER:
        if status in seen:
            return status
    return "ok"


@dataclass(slots=True)
class ModelRouter:
    primary: StreamingModelProvider
    settings: Settings
    fallback: StreamingModelProvider | None = None
    # Called with the fallback event payload when a request switches providers.
    # Injected rather than emitted here so the router keeps no session state.
    on_fallback: Callable[[dict], Awaitable[None]] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> ModelRouter:
        primary = _provider_for(settings.provider.type, settings)
        fallback = None
        name = settings.provider.fallback.strip()
        # Off unless asked for, and never the same provider as the primary: a
        # "fallback" that retries the same endpoint is a retry, and the provider
        # layer already does those.
        if name and name != settings.provider.type and settings.provider.fallback_model.strip():
            fallback = _provider_for(name, settings)
        return cls(primary=primary, settings=settings, fallback=fallback)

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
        # The configured check lives in `_complete_with`, per provider, so an
        # unconfigured primary can hand over to a configured fallback instead of
        # failing the run before either is tried.
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
        provider = self.primary
        try:
            return await self._complete_with(provider, request, on_text_delta)
        except (ProviderError, ProviderNotConfigured) as exc:
            fallback = self._usable_fallback(exc)
            if fallback is None:
                raise
            # Bound here: Python unbinds the exception name at the end of the
            # handler, so reading it below would be an undefined name.
            failure = str(exc)
        # Retried on the fallback with its own model: the primary's model name
        # means nothing to a different provider, and sending it would fail in a
        # way that looks like the fallback is broken.
        fallback_request = replace(request, model=self.settings.provider.fallback_model)
        if self.on_fallback is not None:
            fallback_name = getattr(fallback, "provider_name", fallback.__class__.__name__)
            primary_name = getattr(self.primary, "provider_name", self.primary.__class__.__name__)
            await self.on_fallback(
                {
                    "type": "provider.fallback",
                    "purpose": purpose,
                    "primary": primary_name,
                    "fallback": fallback_name,
                    "model": fallback_request.model,
                    "error": failure,
                    "message": (
                        f"Primary provider {primary_name!r} was unavailable; answered with "
                        f"{fallback_name!r} ({fallback_request.model})."
                    ),
                }
            )
        return await self._complete_with(fallback, fallback_request, on_text_delta)

    def _usable_fallback(self, exc: Exception) -> StreamingModelProvider | None:
        """Only an unreachable provider justifies answering from another one.

        A capability error means the *request* is wrong for this provider, and a
        second provider with different declared capabilities would answer it by
        silently changing what the model can do. Context overflow has its own
        recovery. Both must reach the caller unchanged.
        """
        if self.fallback is None or isinstance(exc, ProviderCapabilityError | ContextOverflowError):
            return None
        return self.fallback

    async def _complete_with(
        self,
        provider: StreamingModelProvider,
        request: CompletionRequest,
        on_text_delta: Callable[[str], Awaitable[None]] | None,
    ) -> CompletionResult:
        is_configured = getattr(provider, "is_configured", None)
        if callable(is_configured) and not is_configured():
            raise ProviderNotConfigured("The model provider is not configured. Set an API key and retry.")
        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        usage = Usage()
        model_name = request.model
        async for event in provider.stream_complete(request):
            if event.type == "text_delta":
                text_parts.append(event.text)
                if on_text_delta is not None:
                    await on_text_delta(event.text)
            elif event.type == "tool_call" and event.tool_call is not None:
                tool_calls.append(event.tool_call)
            elif event.type == "done":
                usage = event.usage or usage
                model_name = event.model or model_name
        provider_name = getattr(provider, "provider_name", provider.__class__.__name__)
        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            model=model_name,
            provider=provider_name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost=self._estimate_cost(
                provider_name, model_name, request.model, usage
            ),
        )

    def _estimate_cost(self, provider: str, responded: str, requested: str, usage: Usage) -> float:
        """Price a call, falling back to the model that was asked for.

        Providers may answer under a different name than the one requested —
        DeepSeek serves `deepseek-chat` as `deepseek-v4-flash` — and the price
        table is configured against the name the user configured. Pricing the
        response name alone silently misses and reports $0.00 for a call that
        was billed, which is worse than an approximate number because it looks
        like a fact.
        """
        for name in (responded, requested):
            if not name:
                continue
            price = price_for(provider=provider, model=name, prices=self.settings.pricing.model_prices)
            if price is not None:
                return estimate_cost(
                    provider=provider,
                    model=name,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    prices=self.settings.pricing.model_prices,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                )
        return 0.0

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

    async def probe_routes(self) -> dict:
        """Probe every route, not just `main`.

        A single-model probe answers "is the provider reachable", which is not
        the question a three-route setup asks. `summarizer` is routinely a
        different, cheaper model, and it is the one most likely to be
        misconfigured precisely because nothing exercises it until a compaction
        fires mid-run — the worst moment to discover the model does not exist.

        Each route is probed with the tool support its own capability declares.
        Probing a summarizer with tools when its profile says `tool_calling=false`
        would report a configuration failure that is really the probe's fault.

        Routes sharing a model are probed once. The point is to find broken
        configuration, not to pay for the same request three times; the shared
        result is reported under every route that uses it.
        """
        results: dict[str, dict] = {}
        by_model: dict[tuple[str, bool], dict] = {}
        for purpose in ("main", "reviewer", "summarizer"):
            model = self.model_for_purpose(purpose)
            tools = self.capability_for_purpose(purpose).tool_calling
            key = (model, tools)
            if key not in by_model:
                by_model[key] = await self.probe(model=model, tools=tools)
            probed = by_model[key]
            results[purpose] = {
                "model": model,
                "tools": tools,
                "status": probed.get("status", "error"),
                "latency_ms": probed.get("latency_ms", 0),
                "checks": probed.get("checks", []),
            }
        return {
            "schema_version": 1,
            # Worst route wins: a setup where compaction cannot run is not
            # healthy just because two routes out of three answered.
            "status": _worst_status(result["status"] for result in results.values()),
            "probes": len(by_model),
            "routes": results,
        }

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
                # Reported as the empty string when off, so the CLI can tell
                # "no fallback configured" from "configured but unnamed".
                "fallback": (
                    getattr(self.fallback, "provider_name", self.fallback.__class__.__name__)
                    if self.fallback is not None
                    else ""
                ),
                "fallback_model": self.settings.provider.fallback_model if self.fallback is not None else "",
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


def _provider_for(name: str, settings: Settings) -> StreamingModelProvider:
    if name == "anthropic":
        return AnthropicProvider(settings.anthropic)
    return OpenAICompatibleProvider(settings.openai_compatible)
