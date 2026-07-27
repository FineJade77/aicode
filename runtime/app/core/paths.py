from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path


def is_protected_path(rel_path: str, protected_paths: list[str]) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    for pattern in protected_paths:
        normalized_pattern = pattern.replace("\\", "/").lstrip("./")
        if fnmatch(normalized, normalized_pattern):
            return True
        if "/" not in normalized_pattern and Path(normalized).name == normalized_pattern:
            return True
    return False
