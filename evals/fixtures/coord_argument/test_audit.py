import pytest

from admin import role_granted
from audit import record
from billing import charge_declined
from login import failed_login


def test_severity_is_required():
    with pytest.raises(TypeError):
        record("some.action", "detail")


def test_each_caller_declares_its_own_severity():
    assert failed_login("ada")["severity"] == "warning"
    assert charge_declined("inv-1")["severity"] == "error"
    assert role_granted("ada")["severity"] == "critical"


def test_the_rest_of_the_entry_is_unchanged():
    entry = failed_login("ada")
    assert entry["action"] == "login.failed"
    assert entry["detail"] == "ada"
