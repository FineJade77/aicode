from models import User
from serializer import serialize


def test_user_carries_an_email():
    user = User(id=1, name="Ada", email="ada@example.com")
    assert user.email == "ada@example.com"


def test_email_is_serialized():
    user = User(id=1, name="Ada", email="ada@example.com")
    assert serialize(user) == {"id": 1, "name": "Ada", "email": "ada@example.com"}
