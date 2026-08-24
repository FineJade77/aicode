"""A pipeline that upper-cases and rejects blanks."""

from base import Pipeline


class StrictPipeline(Pipeline):
    def _validate(self, item):
        return bool(item and item.strip())

    def _apply(self, item):
        return item.strip().upper()
