from __future__ import annotations

import os

from pydantic import BaseModel


class ModelSettings(BaseModel):
    default: str = "gpt-5"
    planner: str = "gpt-5-high"
    coder: str = "gpt-5"
    reviewer: str = "gpt-5"
    summarizer: str = "gpt-5-mini"


class OpenAICompatibleSettings(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    timeout_seconds: float = 60.0


class Settings(BaseModel):
    app_name: str = "aicode-runtime"
    default_language: str = "zh-CN"
    version: str = "0.1.0"
    models: ModelSettings = ModelSettings()
    openai_compatible: OpenAICompatibleSettings = OpenAICompatibleSettings()

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
                reviewer=os.getenv("AICODE_MODEL_REVIEWER", "gpt-5"),
                summarizer=os.getenv("AICODE_MODEL_SUMMARIZER", "gpt-5-mini"),
            ),
            openai_compatible=OpenAICompatibleSettings(
                base_url=os.getenv("AICODE_OPENAI_BASE_URL", "https://api.openai.com/v1"),
                api_key_env=os.getenv("AICODE_OPENAI_API_KEY_ENV", "OPENAI_API_KEY"),
                timeout_seconds=float(os.getenv("AICODE_OPENAI_TIMEOUT_SECONDS", "60")),
            ),
        )


settings = Settings.from_env()
