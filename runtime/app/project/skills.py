"""Named instruction packs the model can load on demand.

A skill is a markdown file with a name and a one-line description. The
descriptions go into the system prompt so the model knows what exists; the
bodies do not, because a repository with twenty skills would spend the context
window on instructions for the nineteen it is not doing.

Two sources, and the difference between them is trust:

- `~/.aicode/skills/` is the user's own, and is read wherever they work.
- `<workspace>/.aicode/skills/` ships with the repository, which means a clone
  can propose instructions. Those are loaded only in a trusted workspace, the
  same answer hooks and MCP servers already give, and even then they arrive
  under the disclaimer `.aicode/rules.md` established: project text is guidance
  and cannot widen what the Agent is allowed to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# One skill is an instruction sheet, not a document set. The cap keeps a single
# file from consuming the turn's budget, and truncation is reported rather than
# silent so a skill that outgrew it is visible.
MAX_SKILL_BODY_CHARS = 16_000
MAX_DESCRIPTION_CHARS = 300
MAX_SKILLS = 64
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    source: str  # "user" | "project"
    path: Path

    def qualified(self) -> str:
        """Project skills are namespaced so a repository cannot shadow a user's.

        Without this, a clone could define `deploy` and quietly take over the
        name the user already had for their own.
        """
        return self.name if self.source == "user" else f"project:{self.name}"


def user_skills_dir() -> Path:
    import os

    home = os.getenv("AICODE_HOME")
    root = Path(home) if home else Path.home() / ".aicode"
    return root / "skills"


def discover_skills(workspace: Path, *, trust_level: str = "trusted") -> list[Skill]:
    """List the skills available for this workspace, user's first.

    Ordering is deterministic and user-before-project, so a listing in the
    prompt does not shuffle between turns and reads in order of authority.
    """
    skills: list[Skill] = []
    skills.extend(_read_dir(user_skills_dir(), "user"))
    if trust_level == "trusted":
        skills.extend(_read_dir(Path(workspace) / ".aicode" / "skills", "project"))
    return skills[:MAX_SKILLS]


def load_skill(workspace: Path, name: str, *, trust_level: str = "trusted") -> tuple[Skill, str] | None:
    """Return a skill and its body, or None when no such skill is available.

    Resolved through `discover_skills` rather than by joining the name onto a
    path: a name that reached the filesystem directly would be a traversal
    waiting to happen, and the qualified form already distinguishes the two
    sources.
    """
    for skill in discover_skills(workspace, trust_level=trust_level):
        if name in {skill.qualified(), skill.name}:
            return skill, _body(skill.path)
    return None


def _read_dir(root: Path, source: str) -> list[Skill]:
    if not root.is_dir():
        return []
    found: list[Skill] = []
    for entry in sorted(root.iterdir()):
        path = entry / "SKILL.md" if entry.is_dir() else entry
        if path.suffix != ".md" or not path.is_file():
            continue
        name = entry.name if entry.is_dir() else entry.stem
        if not NAME_PATTERN.match(name):
            # Skipped rather than sanitised: a name aicode had to rewrite is not
            # the name the author will type when it does not work.
            continue
        found.append(
            Skill(
                name=name,
                description=_description(path) or f"{name} skill",
                source=source,
                path=path,
            )
        )
    return found


def _description(path: Path) -> str:
    """Read the one-line description from frontmatter, or the first heading.

    Frontmatter is a convention, not a requirement: a skill that is just a
    markdown file still works, because the point is to make writing one cheap.
    """
    try:
        text = path.read_text("utf-8", errors="replace")[:4_000]
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip().casefold() == "description":
                    return value.strip()[:MAX_DESCRIPTION_CHARS]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:MAX_DESCRIPTION_CHARS]
        if stripped and not stripped.startswith("---"):
            return stripped[:MAX_DESCRIPTION_CHARS]
    return ""


def _body(path: Path) -> str:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    text = text.strip()
    if len(text) > MAX_SKILL_BODY_CHARS:
        # Said out loud: a skill silently cut in half reads as a complete
        # instruction sheet that happens to be missing its last step.
        return text[:MAX_SKILL_BODY_CHARS] + "\n\n[skill truncated: exceeded the size limit]"
    return text
