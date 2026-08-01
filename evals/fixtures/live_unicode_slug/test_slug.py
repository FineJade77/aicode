from slug import slugify


def test_ascii_title():
    assert slugify("Hello There World") == "hello-there-world"
