from __future__ import annotations

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "aicode-runtime"
    default_language: str = "zh-CN"
    version: str = "0.1.0"


settings = Settings()
