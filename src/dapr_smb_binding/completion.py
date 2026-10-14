"""The write side of the completion contract: partners never see half-written files.

Write order: temp file (exclusive create, no sharing) -> close -> verify size ->
rename to the final name (atomic on the server) -> optional completion marker.
See CONTRACT.md, "Completion settings".
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time
from collections.abc import Callable

from . import metrics
from .config import OnTargetExists, TempFileMode, WriteOptions
from .errors import Kind, SmbError
from .fs import FileSystem, join, split

log = logging.getLogger(__name__)

MAX_UNIQUE_ATTEMPTS = 1000
# Cleanup lists a directory, so do it at most this often per directory.
CLEANUP_MIN_INTERVAL = 60.0
MARKER_ATTEMPTS = 2


def apply_pattern(pattern: str, name: str) -> str:
    return pattern.replace("{name}", name)


def pattern_glob(pattern: str) -> str:
    """The glob matching every name a {name} pattern can produce."""
    return pattern.replace("{name}", "*")


def unique_candidates(name: str):
    """a.xml -> a_1.xml, a_2.xml, ... (dotfiles keep their leading dot)."""
    head = 1 if name.startswith(".") else 0
    dot = name.rfind(".")
    stem, ext = (name[:dot], name[dot:]) if dot > head else (name, "")
    for n in range(1, MAX_UNIQUE_ATTEMPTS + 1):
        yield f"{stem}_{n}{ext}"


def move(fs: FileSystem, src: str, dst: str, policy: OnTargetExists) -> str:
    """Rename src to dst per policy; returns the final path. Never silently overwrites under "fail"."""
    if policy is OnTargetExists.OVERWRITE:
        fs.replace(src, dst)
        return dst
    try:
        fs.rename(src, dst)
        return dst
    except SmbError as e:
        if e.kind is not Kind.EXISTS or policy is OnTargetExists.FAIL:
            raise
    d, name = split(dst)
    for cand in unique_candidates(name):
        target = join(d, cand)
        try:
            fs.rename(src, target)
            return target
        except SmbError as e:
            if e.kind is not Kind.EXISTS:
                raise
    raise SmbError(Kind.EXISTS, f"no free unique name after {MAX_UNIQUE_ATTEMPTS} attempts", path=dst)


class Writer:
    def __init__(
        self,
        fs: FileSystem,
        opts: WriteOptions,
        *,
        labels: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.fs = fs
        self.opts = opts
        self._labels = labels or {"component": "default", "target": ""}
        self._clock = clock
        self._last_cleanup: dict[str, float] = {}
        self._cleanup_lock = threading.Lock()

    def temp_path(self, path: str) -> str:
        d, name = split(path)
        if self.opts.temp_file_mode is TempFileMode.FOLDER:
            return join(d, self.opts.temp_folder, name)
        return join(d, apply_pattern(self.opts.temp_file_name_pattern, name))

    def is_own_temp_name(self, name: str) -> bool:
        return self.opts.temp_file_mode is TempFileMode.NAME and fnmatch.fnmatchcase(
            name, pattern_glob(self.opts.temp_file_name_pattern)
        )

    def create(self, path: str, data: bytes, on_target_exists: OnTargetExists | None = None) -> str:
        """Write data so that path appears complete or not at all. Returns the final path."""
        policy = on_target_exists or self.opts.on_target_exists
        d, _ = split(path)
        # Like the SFTP binding, create missing parent directories.
        self.fs.makedirs(d)

        if self.opts.temp_file_mode is TempFileMode.NONE:
            final = self._create_direct(path, data, policy)
        else:
            final = self._create_via_temp(path, data, policy)

        if self.opts.completion_marker_pattern:
            self._write_marker(final)

        self.maybe_cleanup(path)
        return final

    def _write_marker(self, final: str) -> None:
        """Write the completion marker, retrying once.

        The file is already final at this point, so a failure must not read as "nothing
        happened": the caller would resend the file and, under onTargetExists "fail", get
        EXISTS for a file the partner is still waiting on a marker for.
        """
        fd, fname = split(final)
        marker = join(fd, apply_pattern(self.opts.completion_marker_pattern, fname))
        last: SmbError | None = None
        for _ in range(MARKER_ATTEMPTS):
            try:
                self.fs.write(marker, b"", exclusive_create=False)
                return
            except SmbError as e:
                last = e
                log.warning("writing completion marker %r failed: %s", marker, e)
        assert last is not None
        raise SmbError(
            Kind.OTHER,
            f'"{final}" was written, but its completion marker "{marker}" could not be ({last.message}). '
            "Do not resend the file: the partner will not pick it up until the marker exists",
            path=final,
            status=last.status,
        ) from last

    def _create_direct(self, path: str, data: bytes, policy: OnTargetExists) -> str:
        if policy is OnTargetExists.OVERWRITE:
            self.fs.write(path, data, exclusive_create=False)
            return path
        try:
            self.fs.write(path, data, exclusive_create=True)
            return path
        except SmbError as e:
            if e.kind is not Kind.EXISTS or policy is OnTargetExists.FAIL:
                raise
        d, name = split(path)
        for cand in unique_candidates(name):
            target = join(d, cand)
            try:
                self.fs.write(target, data, exclusive_create=True)
                return target
            except SmbError as e:
                if e.kind is not Kind.EXISTS:
                    raise
        raise SmbError(Kind.EXISTS, f"no free unique name after {MAX_UNIQUE_ATTEMPTS} attempts", path=path)

    def _create_via_temp(self, path: str, data: bytes, policy: OnTargetExists) -> str:
        tmp = self.temp_path(path)
        if self.opts.temp_file_mode is TempFileMode.FOLDER:
            self.fs.makedirs(split(tmp)[0])

        try:
            self.fs.write(tmp, data, exclusive_create=True)
        except SmbError as e:
            if e.kind is not Kind.EXISTS:
                raise
            if not self._remove_if_stale(tmp):
                raise SmbError(
                    Kind.EXISTS,
                    f'temp file "{tmp}" already exists: another write of the same name is in progress, '
                    "or an earlier one crashed (set staleTempFileAge to clean those up)",
                    path=path,
                ) from e
            self.fs.write(tmp, data, exclusive_create=True)

        try:
            size = self.fs.stat(tmp).size
            if size != len(data):
                raise SmbError(Kind.OTHER, f"temp file has {size} bytes after writing {len(data)}", path=tmp)
            return move(self.fs, tmp, path, policy)
        except BaseException:
            self._discard(tmp)
            raise

    def _discard(self, tmp: str) -> None:
        try:
            self.fs.remove(tmp)
        except Exception as e:  # noqa: BLE001
            log.warning("could not remove temp file %r after a failed write: %s", tmp, e)

    def _remove_if_stale(self, tmp: str) -> bool:
        age = self.opts.stale_temp_file_age
        if age is None:
            return False
        try:
            e = self.fs.stat(tmp)
        except SmbError as err:
            return err.kind is Kind.NOT_FOUND
        if self._clock() - e.mtime.timestamp() < age:
            return False
        self.fs.remove(tmp)
        metrics.STALE_TEMP_FILES_REMOVED.labels(**self._labels).inc()
        log.warning("removed stale temp file %r (older than staleTempFileAge)", tmp)
        return True

    def maybe_cleanup(self, path: str) -> None:
        """Delete orphaned temp files (a crash between write and rename) near path.

        Only touches files matching our own temp pattern, or files in our temp folder.
        """
        age = self.opts.stale_temp_file_age
        if age is None or self.opts.temp_file_mode is TempFileMode.NONE:
            return
        d = split(self.temp_path(path))[0]
        now = self._clock()
        with self._cleanup_lock:
            if now - self._last_cleanup.get(d, 0.0) < min(age, CLEANUP_MIN_INTERVAL):
                return
            self._last_cleanup[d] = now
        try:
            for e in self.fs.listdir(d):
                if e.is_dir or now - e.mtime.timestamp() < age:
                    continue
                if self.opts.temp_file_mode is TempFileMode.NAME and not self.is_own_temp_name(e.name):
                    continue
                self.fs.remove(join(d, e.name))
                metrics.STALE_TEMP_FILES_REMOVED.labels(**self._labels).inc()
                log.warning("removed stale temp file %r (older than staleTempFileAge)", join(d, e.name))
        except SmbError as e:
            log.warning("stale temp file cleanup in %r failed: %s", d, e)
