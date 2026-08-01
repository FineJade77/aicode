"""Configuration merging.

`merge(base, override)` layers `override` on top of `base`:

- Nested mappings are merged recursively, key by key.
- Any non-mapping value in `override` replaces the base value outright. Lists are
  values, not containers — they are replaced, never concatenated.
- Neither input is mutated.
"""


def merge(base: dict, override: dict) -> dict:
    result = dict(base)
    result.update(override)
    return result
