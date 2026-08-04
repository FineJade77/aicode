"""Walk the chain, reporting where it stops.

Each module names only its own successor, so the route is not visible from any
single file.
"""

import importlib


def walk():
    name = "start"
    seen = []
    while name:
        module = importlib.import_module(name)
        seen.append((name, module.VALUE))
        name = module.NEXT
    return seen


def first_unset():
    for name, value in walk():
        if value == 0:
            return name
    return ""


if __name__ == "__main__":
    print(first_unset() or "ok")
