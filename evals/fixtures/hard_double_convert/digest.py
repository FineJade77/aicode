"""Group stored events by calendar day (UTC)."""

from collections import defaultdict


def by_day(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["at"].date().isoformat()].append(event["name"])
    return dict(grouped)
