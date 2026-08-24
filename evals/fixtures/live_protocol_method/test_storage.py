import pytest

from file_storage import FileStorage
from memory_storage import MemoryStorage
from storage import Storage


def backends(tmp_path):
    return [MemoryStorage(), FileStorage(tmp_path / "store")]


@pytest.mark.parametrize("index", [0, 1])
def test_delete_removes_the_key(tmp_path, index):
    backend = backends(tmp_path)[index]
    backend.put("a", "1")
    backend.delete("a")
    assert backend.get("a") is None


@pytest.mark.parametrize("index", [0, 1])
def test_delete_is_forgiving(tmp_path, index):
    backend = backends(tmp_path)[index]
    backend.delete("missing")


def test_the_protocol_declares_delete():
    assert "delete" in Storage.__dict__ or hasattr(Storage, "delete")
