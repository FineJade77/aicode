"""CSV export. See SPEC.md for the required format."""

COLUMNS = ("id", "name", "notes")


def to_csv(rows):
    lines = ["id,name,notes"]
    for row in rows:
        lines.append(",".join(f'"{row.get(column)}"' for column in COLUMNS))
    return "\n".join(lines)
