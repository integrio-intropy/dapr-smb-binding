"""A FileSystem on a local directory with SMB's semantics, for unit tests.

- rename refuses to overwrite (EXISTS);
- paths in `open_by_partner` behave like a file another client holds open:
  an exclusive read, a rename or a delete of it fails with SHARING_VIOLATION;
- `fail_next` injects errors; `calls` records every operation.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib

from dapr_smb_binding.errors import (
    STATUS_OBJECT_NAME_COLLISION,
    STATUS_OBJECT_NAME_NOT_FOUND,
    STATUS_SHARING_VIOLATION,
    Kind,
    SmbError,
)
from dapr_smb_binding.fs import Entry, join


class LocalFS:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.open_by_partner: set[str] = set()
        self.fail_next: list[SmbError] = []
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def _p(self, path: str) -> pathlib.Path:
        return self.root / join(path)

    def _hook(self, op: str, path: str) -> None:
        self.calls.append((op, path))
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _missing(self, path: str) -> SmbError:
        return SmbError(Kind.NOT_FOUND, "No such file or directory", status=STATUS_OBJECT_NAME_NOT_FOUND)

    def _check_open(self, path: str) -> None:
        if join(path) in self.open_by_partner:
            raise SmbError(Kind.SHARING_VIOLATION, "sharing violation", status=STATUS_SHARING_VIOLATION)

    def listdir(self, path: str) -> list[Entry]:
        self._hook("list", path)
        p = self._p(path)
        if not p.is_dir():
            raise self._missing(path)
        return [
            Entry(c.name, c.is_dir(), c.stat().st_size if c.is_file() else 0,
                  dt.datetime.fromtimestamp(c.stat().st_mtime, dt.UTC))
            for c in p.iterdir()
        ]  # fmt: skip

    def read(self, path: str, *, exclusive: bool = False) -> bytes:
        self._hook("read", path)
        if exclusive:
            self._check_open(path)
        if not self._p(path).is_file():
            raise self._missing(path)
        return self._p(path).read_bytes()

    def create(self, path: str, data: bytes) -> None:
        self._hook("write", path)
        p = self._p(path)
        if p.exists():
            raise SmbError(Kind.EXISTS, "exists", status=STATUS_OBJECT_NAME_COLLISION)
        if not p.parent.is_dir():
            raise self._missing(path)
        p.write_bytes(data)

    def rename(self, src: str, dst: str) -> None:
        self._hook("rename", src)
        self._check_open(src)
        s, d = self._p(src), self._p(dst)
        if not s.exists():
            raise self._missing(src)
        if d.exists():
            raise SmbError(Kind.EXISTS, "exists", status=STATUS_OBJECT_NAME_COLLISION)
        s.rename(d)

    def remove(self, path: str) -> None:
        self._hook("remove", path)
        self._check_open(path)
        if not self._p(path).is_file():
            raise self._missing(path)
        self._p(path).unlink()

    def makedirs(self, path: str) -> None:
        self._hook("makedirs", path)
        self._p(path).mkdir(parents=True, exist_ok=True)

    def ping(self) -> None:
        self._hook("ping", "")

    def close(self) -> None:
        self.closed = True

    # ---------------------------------------------------------------- test helpers

    def put(self, path: str, data: bytes = b"x", age: float = 0.0) -> None:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if age:
            t = p.stat().st_mtime - age
            os.utime(p, (t, t))

    def files(self, path: str = "") -> list[str]:
        base = self._p(path)
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())
