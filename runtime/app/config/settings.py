from __future__ import annotations

import os

from pydantic import BaseModel, Field

from app.usage.pricing import ModelPrice, parse_model_prices


class ModelSettings(BaseModel):
    default: str = "gpt-5"
    planner: str = "gpt-5-high"
    coder: str = "gpt-5"
    main: str = "gpt-5"
    reviewer: str = "gpt-5"
    summarizer: str = "gpt-5-mini"


class OpenAICompatibleSettings(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: float = 60.0


class ProviderSettings(BaseModel):
    type: str = "openai_compatible"  # openai_compatible | anthropic


class AnthropicSettings(BaseModel):
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: float = 120.0


class PricingSettings(BaseModel):
    currency: str = "USD"
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)


class Settings(BaseModel):
    app_name: str = "aicode-runtime"
    default_language: str = "zh-CN"
    version: str = "0.1.0"
    models: ModelSettings = ModelSettings()
    openai_compatible: OpenAICompatibleSettings = OpenAICompatibleSettings()
    provider: ProviderSettings = ProviderSettings()
    anthropic: AnthropicSettings = AnthropicSettings()
    pricing: PricingSettings = PricingSettings()

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=os.getenv("AICODE_RUNTIME_NAME", "aicode-runtime"),
            default_language=os.getenv("AICODE_DEFAULT_LANGUAGE", "zh-CN"),
            version=os.getenv("AICODE_RUNTIME_VERSION", "0.1.0"),
            models=ModelSettings(
                default=os.getenv("AICODE_MODEL_DEFAULT", "gpt-5"),
                planner=os.getenv("AICODE_MODEL_PLANNER", "gpt-5-high"),
                coder=os.getenv("AICODE_MODEL_CODER", "gpt-5"),
                main=os.getenv("AICODE_MODEL_MAIN", os.getenv("AICODE_MODEL_CODER", "gpt-5")),
                reviewer=os.getenv("AICODE_MODEL_REVIEWER", "gpt-5"),
                summarizer=os.getenv("AICODE_MODEL_SUMMARIZER", "gpt-5-mini"),
            ),
            openai_compatible=OpenAICompatibleSettings(
                base_url=os.getenv("AICODE_OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key_env=os.getenv("AICODE_OPENAI_API_KEY_ENV", "OPENAI_API_KEY"),
                timeout_seconds=float(os.getenv("AICODE_OPENAI_TIMEOUT_SECONDS", "60")),
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
        )


settings = Settings.from_env()
