"""Infrastructure adapters implementing Agent Core and Application ports."""

"""Infrastructure adapters used by the runtime composition root.

Keep this package initializer intentionally small.  Import concrete adapters
from their modules so loading one adapter does not eagerly load every other
infrastructure dependency.
"""
