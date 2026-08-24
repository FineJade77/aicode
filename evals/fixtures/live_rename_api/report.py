"""Reporting built on the store."""

from store import fetch_all


def build_report() -> str:
    return ", ".join(record["label"] for record in fetch_all())
