"""Review Stream handler."""

from contracts import Result


def handle(records):
    """Summarise review_stream records.

    Returns a Result whose `total` is the number of records processed.
    """
    total = 0
    for record in records:
        if record.get("kind") != "review_stream":
            continue
        total += 1
    return Result(name="review_stream", total=total)
