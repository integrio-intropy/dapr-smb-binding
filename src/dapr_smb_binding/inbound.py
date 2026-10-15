"""The input side: deliver partner files only once they are complete.

A file is read with an exclusive open, which the server refuses with
STATUS_SHARING_VIOLATION while the partner still has it open; it is then
skipped until the next poll. It stays in inboundPath until the app answers, and
is then moved to processedFolder or errorFolder. A crash at any point leaves it
in inboundPath, so it is delivered again: at least once.

This assumes one replica per input component. Several replicas would deliver
the same file more than once.
"""

from __future__ import annotations

import fnmatch
import logging
import mimetypes
from dataclasses import dataclass

from .config import Config
from .errors import Kind, SmbError
from .fs import Entry, FileSystem, join

log = logging.getLogger(__name__)


@dataclass
class Message:
    name: str
    data: bytes
    metadata: dict[str, str]
    content_type: str


class Inbound:
    def __init__(self, cfg: Config, fs: FileSystem, *, max_file_size: int):
        self.cfg = cfg
        self.fs = fs
        self.max_file_size = max_file_size
        # Our own temp files are never picked up, whatever filePattern says.
        self._ignore = (*cfg.ignore_patterns, cfg.temp_file_name_pattern.replace("{name}", "*"))
        self._made: set[str] = set()

    def poll(self) -> list[Entry]:
        """The files ready to look at, oldest first."""
        files = [
            e
            for e in self.fs.listdir(self.cfg.inbound_path)
            if not e.is_dir
            and any(fnmatch.fnmatchcase(e.name, p) for p in self.cfg.file_patterns)
            and not any(fnmatch.fnmatchcase(e.name, p) for p in self._ignore)
        ]
        return sorted(files, key=lambda e: (e.mtime, e.name))

    def load(self, e: Entry) -> Message | None:
        """Read a file, or None to skip it for now."""
        path = join(self.cfg.inbound_path, e.name)
        if e.size > self.max_file_size:
            log.error(
                "%r is %d bytes, over the %d-byte message limit; moving it to %r",
                path,
                e.size,
                self.max_file_size,
                self.cfg.error_folder,
            )
            self.complete(e.name, ok=False)
            return None
        try:
            data = self.fs.read(path, exclusive=True)
        except SmbError as err:
            if err.kind is Kind.SHARING_VIOLATION:
                log.debug("skipping %r: still open by another client", path)
                return None
            if err.kind is Kind.NOT_FOUND:
                return None
            raise
        md = {
            "fileName": e.name,
            "size": str(len(data)),
            "modTime": e.mtime.isoformat().replace("+00:00", "Z"),
        }
        return Message(e.name, data, md, mimetypes.guess_type(e.name)[0] or "application/octet-stream")

    def complete(self, name: str, *, ok: bool, error: str = "") -> None:
        """Move the file out of inboundPath once the app has answered."""
        if not ok:
            log.warning("%r rejected: %s", name, error or "no reason given")
        folder = join(self.cfg.inbound_path, self.cfg.processed_folder if ok else self.cfg.error_folder)
        if folder not in self._made:
            self.fs.makedirs(folder)
            self._made.add(folder)
        src = join(self.cfg.inbound_path, name)
        stem, dot, ext = name.rpartition(".") if "." in name.lstrip(".") else (name, "", "")
        # Partners reuse names, so keep both: a.xml, a_1.xml, a_2.xml, ...
        for n in range(1000):
            target = name if n == 0 else f"{stem}_{n}{dot}{ext}"
            try:
                self.fs.rename(src, join(folder, target))
                return
            except SmbError as err:
                if err.kind is not Kind.EXISTS:
                    raise
        raise SmbError(Kind.EXISTS, f"no free name for {name!r} in {folder!r}")
