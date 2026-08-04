"""Remote fetching with a fixed retry count."""


def fetch(url, transport):
    last = None
    for _ in range(3):
        result = transport(url)
        if result is not None:
            return result
        last = result
    return last
