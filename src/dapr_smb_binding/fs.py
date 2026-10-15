"""The file operations the binding needs, independent of SMB.

Paths are share-relative with forward slashes. Implementations raise SmbError,
so the binding logic can be tested against a local-directory fake.
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
    def listdir(self, path: str) -> list[Entry]: ...

    def read(self, path: str, *, exclusive: bool = False) -> bytes:
        """exclusive=True denies all sharing: SHARING_VIOLATION if anyone else has the file open."""

    def create(self, path: str, data: bytes) -> None:
        """Create a file that must not exist yet (EXISTS otherwise), with no sharing while writing."""

    def rename(self, src: str, dst: str) -> None:
        """Atomic; refuses to overwrite (EXISTS)."""

    def remove(self, path: str) -> None: ...

    def makedirs(self, path: str) -> None: ...

    def ping(self) -> None: ...

    def close(self) -> None: ...


def join(*parts: str) -> str:
    return "/".join(x for p in parts for x in p.replace("\\", "/").split("/") if x and x != ".")


def split(path: str) -> tuple[str, str]:
    d, _, n = path.rpartition("/")
    return d, n
