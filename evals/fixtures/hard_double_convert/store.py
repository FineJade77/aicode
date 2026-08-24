"""In-memory event store."""

from datetime import timedelta

# The office runs on UTC+2; records are kept in local time for the digest.
LOCAL_OFFSET = timedelta(hours=2)


class Store:
    def __init__(self):
        self.events = []

    def add(self, name, timestamp):
        self.events.append({"name": name, "at": timestamp + LOCAL_OFFSET})

    def all(self):
        return list(self.events)
