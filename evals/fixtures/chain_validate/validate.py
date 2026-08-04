"""Report the first problem only.

Deliberately one at a time: the next problem is not knowable until this one is
resolved, which is the point of the exercise.
"""

from records import RECORDS

RULES = (
    ("amount must be an int", lambda r: isinstance(r["amount"], int)),
    ("currency must be upper case", lambda r: r["currency"].isupper()),
    ("status must be lower case", lambda r: r["status"].islower()),
    ("owner must be a full name", lambda r: " " in r["owner"]),
)


def first_problem():
    for message, rule in RULES:
        for record in RECORDS:
            if not rule(record):
                return f"{record['id']}: {message}"
    return ""


if __name__ == "__main__":
    print(first_problem() or "ok")
