"""Check loaded settings against the schema."""

from schema import REQUIRED_KEYS


def missing_keys(settings):
    """Return {section: [missing keys]} for every section that is incomplete."""
    report = {}
    for section, values in settings.items():
        absent = [key for key in REQUIRED_KEYS if key not in values]
        if absent:
            report[section] = absent
    return report
