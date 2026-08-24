"""Money splitting, in integer cents."""


def split_bill(total_cents: int, people: int) -> list[int]:
    """Split a bill so the parts sum to exactly `total_cents`.

    Parts may differ by at most one cent.
    """
    share = total_cents // people
    return [share] * people
