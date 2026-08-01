"""Pipeline base class."""


class Pipeline:
    def __init__(self):
        self.applied = []

    def run(self, items):
        results = []
        for item in items:
            if self._validate(item):
                results.append(self._apply(item))
        return results

    def _validate(self, item):
        return item is not None

    def _apply(self, item):
        """Transform one item. Subclasses override this.

        Implementations must record what they applied so the audit layer can
        report it.
        """
        self.applied.append(item)
        return item
