"""A FileSystem on a local directory with SMB's semantics, for unit tests.

- rename refuses to overwrite (EXISTS), replace overwrites;
- paths in `open_by_partner` behave like a file another client holds open
  without delete or read sharing: rename and exclusive read fail with
  SHARING_VIOLATION;
- `fail_next` injects errors.
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

    @staticmethod
    def _nf(path: str) -> SmbError:
        return SmbError(
            Kind.NOT_FOUND, "No such file or directory", path=path, status=STATUS_OBJECT_NAME_NOT_FOUND
        )

    def _locked(self, path: str) -> None:
        if join(path) in self.open_by_partner:
            raise SmbError(
                Kind.SHARING_VIOLATION, "sharing violation", path=path, status=STATUS_SHARING_VIOLATION
            )

    def _entry(self, p: pathlib.Path) -> Entry:
        st = p.stat()
        return Entry(
            p.name,
            p.is_dir(),
            st.st_size if p.is_file() else 0,
            dt.datetime.fromtimestamp(st.st_mtime, dt.UTC),
        )

    def listdir(self, path: str) -> list[Entry]:
        self._hook("list", path)
        p = self._p(path)
        if not p.is_dir():
            raise self._nf(path)
        return [self._entry(c) for c in p.iterdir()]

    def stat(self, path: str) -> Entry:
        self._hook("stat", path)
        p = self._p(path)
        if not p.exists():
            raise self._nf(path)
        return self._entry(p)

    def read(self, path: str, *, exclusive: bool = False) -> bytes:
        self._hook("read", path)
        if exclusive:
            self._locked(path)
        p = self._p(path)
        if not p.is_file():
            raise self._nf(path)
        return p.read_bytes()

    def write(self, path: str, data: bytes, *, exclusive_create: bool) -> None:
        self._hook("write", path)
        p = self._p(path)
        if not p.parent.is_dir():
            raise SmbError(Kind.NOT_FOUND, "parent missing", path=path, status=0xC000003A)
        if exclusive_create and p.exists():
            raise SmbError(Kind.EXISTS, "exists", path=path, status=STATUS_OBJECT_NAME_COLLISION)
        p.write_bytes(data)

    def rename(self, src: str, dst: str) -> None:
        self._hook("rename", src)
        self._locked(src)
        s, d = self._p(src), self._p(dst)
        if not s.exists():
            raise self._nf(src)
        if d.exists():
            raise SmbError(Kind.EXISTS, "exists", path=dst, status=STATUS_OBJECT_NAME_COLLISION)
        s.rename(d)

    def replace(self, src: str, dst: str) -> None:
        self._hook("replace", src)
        self._locked(src)
        s = self._p(src)
        if not s.exists():
            raise self._nf(src)
        os.replace(s, self._p(dst))

    def remove(self, path: str) -> None:
        self._hook("remove", path)
        self._locked(path)
        p = self._p(path)
        if not p.is_file():
            raise self._nf(path)
        p.unlink()

    def makedirs(self, path: str) -> None:
        self._hook("makedirs", path)
        self._p(path).mkdir(parents=True, exist_ok=True)

    def rmdir(self, path: str) -> None:
        self._hook("rmdir", path)
        p = self._p(path)
        if not p.is_dir():
            raise self._nf(path)
        if any(p.iterdir()):
            raise SmbError(Kind.OTHER, "directory not empty", path=path, status=0xC0000101)
        p.rmdir()

    def ping(self) -> None:
        self._hook("ping", "")

    def close(self) -> None:
        self.closed = True

    # test helpers
    def put(self, path: str, data: bytes = b"x", age: float = 0.0) -> pathlib.Path:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if age:
            t = p.stat().st_mtime - age
            os.utime(p, (t, t))
        return p

    def files(self, path: str = "", *, heartbeats: bool = False) -> list[str]:
        base = self._p(path)
        return sorted(
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and (heartbeats or p.name != ".heartbeat")
        )
