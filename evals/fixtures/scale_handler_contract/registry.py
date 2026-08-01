"""Handler registry."""

import importlib

from contracts import Result

HANDLERS = [
    "orders",
    "invoices",
    "shipments",
    "returns",
    "refunds",
    "customers",
    "vendors",
    "products",
    "variants",
    "inventory",
    "warehouses",
    "carriers",
    "tariffs",
    "taxes",
    "discounts",
    "coupons",
    "bundles",
    "reviews",
    "ratings",
    "wishlists",
    "carts",
    "sessions",
    "accounts",
    "roles",
    "permissions",
    "audits",
    "webhooks",
    "exports",
    "imports",
    "reports",
    "alerts",
    "digests",
    "receipts",
    "payouts",
    "disputes",
]


def dispatch(records) -> list[Result]:
    results = []
    for name in HANDLERS:
        module = importlib.import_module(f"handlers.{name}")
        results.append(module.handle(records))
    return results
