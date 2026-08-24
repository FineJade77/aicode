"""Dependency ordering."""


class CycleError(Exception):
    """Raised when the dependency graph cannot be ordered."""


def resolve(deps: dict[str, list[str]]) -> list[str]:
    """Return the nodes in an order where every dependency precedes its dependent.

    `deps[node]` lists the nodes `node` depends on. Raises CycleError when no
    such order exists.
    """
    ordered = []
    for node in sorted(deps):
        for dependency in deps[node]:
            if dependency not in ordered:
                ordered.append(dependency)
        if node not in ordered:
            ordered.append(node)
    return ordered
