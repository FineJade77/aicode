"""Exchange rates, fetched once per currency."""

from cache import memoize

TABLE = {"USD": 1.0, "EUR": 1.1, "GBP": 1.3}


@memoize
def rate_for(base, currency="USD"):
    """Return the conversion rate from `base` into `currency`."""
    return TABLE[currency] / TABLE[base]
