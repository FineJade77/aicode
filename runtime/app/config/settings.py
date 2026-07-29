from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.usage.pricing import ModelPrice, parse_model_prices


class ModelSettings(BaseModel):
    main: str = "gpt-5"
    reviewer: str = "gpt-5"
    summarizer: str = "gpt-5-mini"


class OpenAICompatibleSettings(BaseModel):
    profile: str = Field(default="openai", min_length=1)
    profile_schema_version: Literal[1] = 1
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    auth_mode: Literal["required", "optional", "none"] = "required"
    timeout_seconds: float = Field(default=60.0, gt=0)
    context_window: int = Field(default=32_768, ge=2)
    max_output_tokens: int = Field(default=8_192, ge=1)
    tool_calling: bool = True
    streaming: bool = True
    tokenizer: Literal["chars"] = "chars"
    chars_per_token: float = Field(default=3.5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_token_budget(self) -> "OpenAICompatibleSettings":
        if self.max_output_tokens >= self.context_window:
            raise ValueError("max_output_tokens must be smaller than context_window")
        return self


class ProviderSettings(BaseModel):
    type: str = "openai_compatible"  # openai_compatible | anthropic


class AnthropicSettings(BaseModel):
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: float = 120.0


class PricingSettings(BaseModel):
    currency: str = "USD"
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)


class ExecutionSettings(BaseModel):
    """Where Agent-issued shell commands run.

    `auto` keeps trusted workspaces on the host (fast, full toolchain) and pushes
    untrusted workspaces into the Docker sandbox. `host` restores the pre-sandbox
    behaviour for machines without Docker; `docker` is the strictest setting and
    sandboxes every Agent command regardless of trust.
    """

    agent_bash_backend: Literal["auto", "host", "docker"] = "auto"


class ContextSettings(BaseModel):
    default_context_window: int = 32_768
    default_max_output_tokens: int = 8_192
    reserve_tokens: int = 1_024
    compact_threshold: float = 0.8
    chars_per_token: float = 3.5
    model_context_windows: dict[str, int] = Field(default_factory=dict)
    model_max_output_tokens: dict[str, int] = Field(default_factory=dict)


class Settings(BaseModel):
    app_name: str = "aicode-runtime"
    version: str = "0.1.0"
    models: ModelSettings = ModelSettings()
    openai_compatible: OpenAICompatibleSettings = OpenAICompatibleSettings()
    provider: ProviderSettings = ProviderSettings()
    anthropic: AnthropicSettings = AnthropicSettings()
    pricing: PricingSettings = PricingSettings()
    context: ContextSettings = ContextSettings()
    execution: ExecutionSettings = ExecutionSettings()

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=os.getenv("AICODE_RUNTIME_NAME", "aicode-runtime"),
            version=os.getenv("AICODE_RUNTIME_VERSION", "0.1.0"),
            models=ModelSettings(
                main=os.getenv("AICODE_MODEL_MAIN", os.getenv("AICODE_MODEL_CODER", "gpt-5")),
                reviewer=os.getenv("AICODE_MODEL_REVIEWER", "gpt-5"),
                summarizer=os.getenv("AICODE_MODEL_SUMMARIZER", "gpt-5-mini"),
            ),
            openai_compatible=OpenAICompatibleSettings(
                profile=os.getenv("AICODE_OPENAI_PROFILE", "openai"),
                profile_schema_version=int(os.getenv("AICODE_OPENAI_PROFILE_SCHEMA_VERSION", "1")),  # type: ignore[arg-type]
                base_url=os.getenv("AICODE_OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key_env=os.getenv("AICODE_OPENAI_API_KEY_ENV", "OPENAI_API_KEY"),
                auth_mode=os.getenv("AICODE_OPENAI_AUTH_MODE", "required").strip().casefold(),  # type: ignore[arg-type]
                timeout_seconds=float(os.getenv("AICODE_OPENAI_TIMEOUT_SECONDS", "60")),
                context_window=int(os.getenv("AICODE_OPENAI_CONTEXT_WINDOW", "32768")),
                max_output_tokens=int(os.getenv("AICODE_OPENAI_MAX_OUTPUT_TOKENS", "8192")),
                tool_calling=os.getenv("AICODE_OPENAI_TOOL_CALLING", "true"),  # type: ignore[arg-type]
                streaming=os.getenv("AICODE_OPENAI_STREAMING", "true"),  # type: ignore[arg-type]
                tokenizer=os.getenv("AICODE_OPENAI_TOKENIZER", "chars").strip().casefold(),  # type: ignore[arg-type]
                chars_per_token=float(os.getenv("AICODE_OPENAI_CHARS_PER_TOKEN", "3.5")),
            ),
            provider=ProviderSettings(type=os.getenv("AICODE_PROVIDER_TYPE", "openai_compatible")),
            anthropic=AnthropicSettings(
                base_url=os.getenv("AICODE_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                api_key_env=os.getenv("AICODE_ANTHROPIC_API_KEY_ENV", "ANTHROPIC_API_KEY"),
                timeout_seconds=float(os.getenv("AICODE_ANTHROPIC_TIMEOUT_SECONDS", "120")),
            ),
            pricing=PricingSettings(
                currency=os.getenv("AICODE_PRICING_CURRENCY", "USD"),
                model_prices=parse_model_prices(os.getenv("AICODE_MODEL_PRICES_JSON")),
            ),
            context=ContextSettings(
                default_context_window=_positive_int_env("AICODE_CONTEXT_DEFAULT_WINDOW", 32_768),
                default_max_output_tokens=_positive_int_env("AICODE_CONTEXT_DEFAULT_MAX_OUTPUT_TOKENS", 8_192),
                reserve_tokens=_non_negative_int_env("AICODE_CONTEXT_RESERVE_TOKENS", 1_024),
                compact_threshold=_bounded_float_env("AICODE_CONTEXT_COMPACT_THRESHOLD", 0.8, 0.1, 1.0),
                chars_per_token=_bounded_float_env("AICODE_CONTEXT_CHARS_PER_TOKEN", 3.5, 1.0, 20.0),
                model_context_windows=_parse_positive_int_map(os.getenv("AICODE_MODEL_CONTEXT_WINDOWS_JSON")),
                model_max_output_tokens=_parse_positive_int_map(os.getenv("AICODE_MODEL_MAX_OUTPUT_TOKENS_JSON")),
            ),
            execution=ExecutionSettings(
                agent_bash_backend=_agent_bash_backend_env(),
            ),
        )


def _agent_bash_backend_env() -> str:
    value = os.getenv("AICODE_AGENT_BASH_BACKEND", "auto").strip().casefold()
    return value if value in {"auto", "host", "docker"} else "auto"


def _parse_positive_int_map(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    parsed: dict[str, int] = {}
    for key, value in payload.items():
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue
        if normalized > 0:
            parsed[str(key)] = normalized
    return parsed


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _non_negative_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


settings = Settings.from_env()
