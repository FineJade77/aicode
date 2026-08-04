"""Record de-duplication."""


def dedupe(records):
    """Drop duplicate records, keeping the first occurrence."""
    seen = set()
    out = []
    for record in records:
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        out.append(record)
    return out
