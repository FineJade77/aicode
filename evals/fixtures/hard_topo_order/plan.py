"""Build plan derived from the dependency graph."""

from graph import resolve


def build_order(deps):
    return resolve(deps)
