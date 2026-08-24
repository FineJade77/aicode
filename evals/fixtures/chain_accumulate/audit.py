"""Report the first step whose balance is missing or wrong.

One at a time, because a later balance cannot be judged until the earlier ones
are settled.
"""

from steps import STEPS


def first_bad_step():
    running = 0
    for step in STEPS:
        running += step["delta"]
        if step["balance"] != running:
            return step["name"]
    return ""


if __name__ == "__main__":
    print(first_bad_step() or "ok")
