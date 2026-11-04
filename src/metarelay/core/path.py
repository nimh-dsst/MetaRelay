"""Logical paths and containers: where a record lives, independent of backend.

A ``LogicalPath`` is a tuple of names, for example
``("Fiber study", "Cohort A", "M1")``. Each backend maps it to whatever it has:

- Local and GitHub: nested directories
- LabArchives: notebook, folders, and a page
- eLabFTW: no folders, so tags, categories, or item titles (decided in the
  eLabFTW spike)

The core never assumes a backend can nest folders.
"""

from __future__ import annotations

from typing import Self

import pydantic
from pydantic import ConfigDict, Field, RootModel, field_validator

from metarelay.core.errors import ValidationError

__all__ = ["ROOT", "Container", "LogicalPath"]


class LogicalPath(RootModel[tuple[str, ...]]):
    """An immutable path of names. ``LogicalPath(())`` is the destination root."""

    model_config = ConfigDict(frozen=True)

    root: tuple[str, ...] = ()

    @field_validator("root")
    @classmethod
    def _check_parts(cls, parts: tuple[str, ...]) -> tuple[str, ...]:
        for part in parts:
            if not part.strip():
                raise ValueError("path names cannot be empty")
            if part != part.strip():
                raise ValueError(f"path name {part!r} has leading or trailing spaces")
            if "/" in part or part in {".", ".."}:
                raise ValueError(f"path name {part!r} cannot contain '/' or be '.' or '..'")
        return parts

    def __init__(self, root: tuple[str, ...] | list[str] = ()) -> None:
        # tuple("Study") would silently become ("S", "t", "u", "d", "y").
        if isinstance(root, str):
            raise ValidationError(
                f"LogicalPath takes a tuple of names, not a string. "
                f"Use LogicalPath(({root!r},)) or LogicalPath.parse({root!r})."
            )
        try:
            super().__init__(tuple(root))
        except TypeError as error:
            raise ValidationError(f"LogicalPath takes a tuple of names, not {root!r}.") from error
        except pydantic.ValidationError as error:
            raise ValidationError(f"Invalid path: {error}") from error

    @classmethod
    def parse(cls, text: str) -> Self:
        """Build a path from ``"a/b/c"``. An empty string is the root."""
        return cls(tuple(part for part in text.split("/") if part))

    @property
    def parts(self) -> tuple[str, ...]:
        return self.root

    @property
    def name(self) -> str:
        """The last name in the path, or ``""`` for the root."""
        return self.root[-1] if self.root else ""

    @property
    def parent(self) -> LogicalPath:
        return LogicalPath(self.root[:-1])

    def child(self, name: str) -> LogicalPath:
        return LogicalPath((*self.root, name))

    def __truediv__(self, name: str) -> LogicalPath:
        return self.child(name)

    def __str__(self) -> str:
        return "/".join(self.root)

    def __len__(self) -> int:
        return len(self.root)


ROOT = LogicalPath()


class Container(pydantic.BaseModel):
    """A container at a destination: its logical path plus the backend's own ID.

    ``native_id`` is whatever the backend uses to find it again quickly (a
    LabArchives tree ID, an eLabFTW experiment ID). Local and GitHub can leave
    it ``None`` because the path is the ID.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: LogicalPath = Field(default_factory=LogicalPath)
    native_id: str | None = None

    @property
    def name(self) -> str:
        return self.path.name
