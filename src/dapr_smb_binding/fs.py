"""The file-system operations the binding needs, independent of SMB.

Paths are share-relative with forward slashes. Implementations raise
:class:`~dapr_smb_binding.errors.SmbError` with a :class:`~dapr_smb_binding.errors.Kind`
so the binding logic can be tested against :mod:`dapr_smb_binding.localfs`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int
    mtime: dt.datetime


class FileSystem(Protocol):
    def listdir(self, path: str) -> list[Entry]:
        """List a directory. Never includes "." or ".."."""

    def stat(self, path: str) -> Entry: ...

    def read(self, path: str, *, exclusive: bool = False) -> bytes:
        """Read a whole file. exclusive=True denies all sharing while open, so it
        fails with SHARING_VIOLATION if anyone else has the file open."""

    def write(self, path: str, data: bytes, *, exclusive_create: bool) -> None:
        """Write a whole file. exclusive_create=True fails with EXISTS if the file
        exists, and denies sharing while writing; False truncates or creates."""

    def rename(self, src: str, dst: str) -> None:
        """Atomic rename that refuses to overwrite (EXISTS)."""

    def replace(self, src: str, dst: str) -> None:
        """Atomic rename that overwrites an existing target."""

    def remove(self, path: str) -> None: ...

    def makedirs(self, path: str) -> None:
        """Create a directory and its parents; no error if it exists."""

    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""

    def ping(self) -> None:
        """Verify the session and tree connect are usable."""

    def close(self) -> None: ...


def join(*parts: str) -> str:
    out: list[str] = []
    for p in parts:
        out.extend(x for x in p.replace("\\", "/").split("/") if x and x != ".")
    return "/".join(out)


def split(path: str) -> tuple[str, str]:
    """Split into (directory, name)."""
    if "/" in path:
        d, n = path.rsplit("/", 1)
        return d, n
    return "", path
