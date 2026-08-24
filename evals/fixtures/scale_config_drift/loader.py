"""Load every section's defaults into one settings map."""

import importlib

from registry import SECTIONS


def load_all():
    settings = {}
    for name in SECTIONS:
        module = importlib.import_module(f"sections.{name}")
        settings[module.SECTION] = module.defaults()
    return settings
