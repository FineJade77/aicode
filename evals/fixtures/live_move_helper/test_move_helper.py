import posts
from text_utils import slugify


def test_slugify_lives_in_text_utils():
    assert slugify("Hello There World") == "hello-there-world"


def test_posts_no_longer_defines_it():
    assert "slugify" not in posts.__dict__ or posts.slugify is slugify


def test_permalink_still_works():
    assert posts.permalink("Hello There") == "/posts/hello-there"
