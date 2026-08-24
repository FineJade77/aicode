"""Tiny memoisation helper used across the pricing stack."""

_STORE: dict = {}


def memoize(fn):
    """Cache a function's result per distinct call."""

    def wrapper(*args, **kwargs):
        key = (fn.__name__, args)
        if key not in _STORE:
            _STORE[key] = fn(*args, **kwargs)
        return _STORE[key]

    wrapper.cache_clear = _STORE.clear
    return wrapper
