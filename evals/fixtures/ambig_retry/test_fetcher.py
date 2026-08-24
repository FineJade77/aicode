from fetcher import fetch


def test_it_still_retries_three_times_by_default():
    seen = []

    def transport(url):
        seen.append(url)
        return None

    fetch("http://example.invalid", transport)
    assert len(seen) == 3
