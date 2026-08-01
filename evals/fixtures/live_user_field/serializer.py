"""Wire format for the models."""

from models import User


def serialize(user: User) -> dict:
    return {"id": user.id, "name": user.name}
