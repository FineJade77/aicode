"""Service configuration."""

from dataclasses import dataclass

KNOWN_KEYS = {"host", "port"}


@dataclass
class Config:
    host: str = "localhost"
    port: int = 8080
