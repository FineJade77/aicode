from loader import load_all
from validate import missing_keys


def test_every_section_defines_every_required_key():
    assert missing_keys(load_all()) == {}


def test_all_sections_are_loaded():
    assert len(load_all()) == 30
