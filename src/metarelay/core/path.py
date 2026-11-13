"""Logical paths and containers: where a record lives, independent of backend.

A ``LogicalPath`` is a tuple of names, for example
``("Fiber study", "Cohort A", "M1")``. Each backend maps it to whatever it has:

- Local and GitHub: nested directories
- LabArchives: notebook, folders, and a page
- eLabFTW: no folders, so every container is an entry, tagged with its path
  (docs/adr/0001-elabftw-record-storage.md)

The core never assumes a backend can nest folders.

Names are rejected here only if they are unsafe everywhere (empty, ``/``,
``.``/``..``, control characters such as NUL or a newline). Characters that
are only unsafe on some backends, like ``\\`` or ``:`` on a Windows disk, are
for that backend to reject.
"""

from __future__ import annotations

import unicodedata
from typing import Self

from pydantic import ConfigDict, Field, RootModel, field_validator

from metarelay.core.errors import ValidationError
from metarelay.core.model import MetaRelayModel, TranslatesValidationErrors

__all__ = ["ROOT", "Container", "LogicalPath"]


class LogicalPath(TranslatesValidationErrors, RootModel[tuple[str, ...]]):
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
            if any(unicodedata.category(char) == "Cc" for char in part):
                raise ValueError(f"path name {part!r} contains a control character")
        return parts

    def __init__(self, root: tuple[str, ...] | list[str] = ()) -> None:
        # tuple("Study") would silently become ("S", "t", "u", "d", "y").
        if isinstance(root, str):
            raise ValidationError(
                f"LogicalPath takes a tuple of names, not a string. "
                f"Use LogicalPath(({root!r},)) or LogicalPath.parse({root!r})."
            )
        try:
            names = tuple(root)
        except TypeError as error:
            raise ValidationError(f"LogicalPath takes a tuple of names, not {root!r}.") from error
        super().__init__(names)

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

    # No __len__ on purpose: it would make ROOT falsy, so ``path or default``
    # would replace the root with the default. Use ``len(path.parts)``.


ROOT = LogicalPath()


class Container(MetaRelayModel):
    """A container at a destination: its logical path plus the backend's own ID.

    ``native_id`` is whatever the backend uses to find it again quickly (a
    LabArchives tree ID, an eLabFTW experiment ID). Local and GitHub can leave
    it ``None`` because the path is the ID.
    """

    path: LogicalPath = Field(default_factory=LogicalPath)
    native_id: str | None = None
    holds_records: bool = False
    """True for a container that holds records and can't contain other
    containers (a LabArchives page), made by
    ``ensure_container(path, holds_records=True)`` or by hand. Always
    ``False`` on backends where every container is a folder."""

    @property
    def name(self) -> str:
        return self.path.name
