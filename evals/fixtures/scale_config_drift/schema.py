"""The keys every section is required to define.

A section that omits one of these, or spells one differently, is a bug: the
loader has no way to tell a missing key from an intentionally absent one.
"""

REQUIRED_KEYS = ("enabled", "retry_limit", "timeout_seconds")
