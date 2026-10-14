"""The read side of the completion contract: we never read half-written files.

poll() lists inboundPath, filters, applies the partner's completion rule
(marker and/or stability), and claims each ready file by renaming it into
processing/<instance>/. The rename is atomic, so with several replicas only
one wins, and Windows refuses it with STATUS_SHARING_VIOLATION while the
partner still has the file open: that file is skipped until the next poll.

Each replica keeps a heartbeat file in its processing folder. A replica that has
gone away (crash, rollout: Deployment pods get new names) leaves its claimed but
unacknowledged files behind; once its heartbeat is older than staleClaimAge, the
other replicas return those files to inboundPath. File times cannot be used for
this, because a rename keeps the partner's modification time.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import mimetypes
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import metrics
from .completion import apply_pattern, pattern_glob, unique_candidates
from .config import AfterRead, ClaimMode, ReadOptions, TempFileMode, WriteOptions
from .errors import Kind, SmbError
from .fs import Entry, FileSystem, join

log = logging.getLogger(__name__)

HEARTBEAT_NAME = ".heartbeat"
# How often stale claims are looked for, at most.
SWEEP_INTERVAL = 60.0


@dataclass(frozen=True)
class Claimed:
    name: str  # the partner's file name
    path: str  # where it is now (processing folder, or inbound for claimMode none)
    size: int
    mtime: dt.datetime
    recovered: bool = False

    @property
    def key(self) -> tuple[str, int, float]:
        return (self.name, self.size, self.mtime.timestamp())


@dataclass
class Message:
    claimed: Claimed
    data: bytes
    metadata: dict[str, str]
    content_type: str


class Inbound:
    def __init__(
        self,
        fs: FileSystem,
        opts: ReadOptions,
        write_opts: WriteOptions,
        *,
        instance_id: str,
        max_file_size: int,
        labels: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.fs = fs
        self.opts = opts
        self.instance_id = instance_id
        self.max_file_size = max_file_size
        self._labels = labels or {"component": "default", "target": ""}
        self._clock = clock
        self._lock = threading.Lock()
        # name -> (size, mtime, first seen with that size and mtime)
        self._seen: dict[str, tuple[int, float, float]] = {}
        # claimMode none: files already delivered, so they are not delivered again
        self._delivered: set[tuple[str, int, float]] = set()
        self._dirs_made: set[str] = set()
        # Claimed, but still open by someone when we tried to read: retried next poll.
        self._retry: list[Claimed] = []
        self._last_sweep = float("-inf")
        self._heartbeat_thread: threading.Thread | None = None

        ignore = list(opts.ignore_patterns)
        # Always ignore our own temp files and markers, and the partner's markers.
        if write_opts.temp_file_mode is TempFileMode.NAME:
            ignore.append(pattern_glob(write_opts.temp_file_name_pattern))
        for p in (write_opts.completion_marker_pattern, opts.require_marker_pattern):
            if p:
                ignore.append(pattern_glob(p))
        self._ignore = tuple(ignore)

    # ------------------------------------------------------------------ paths

    @property
    def inbound(self) -> str:
        return self.opts.inbound_path

    @property
    def processing_dir(self) -> str:
        return join(self.inbound, self.opts.processing_folder, self.instance_id)

    def _folder(self, name: str) -> str:
        return join(self.inbound, name)

    def _makedirs(self, d: str) -> None:
        if d not in self._dirs_made:
            self.fs.makedirs(d)
            self._dirs_made.add(d)

    @property
    def _takes_over_claims(self) -> bool:
        return self.opts.claim_mode is ClaimMode.RENAME and self.opts.stale_claim_age is not None

    # ------------------------------------------------------------------ heartbeat

    def heartbeat(self) -> None:
        """Touch processing/<instance>/.heartbeat: this replica is alive and owns its claims."""
        try:
            self._makedirs(self.processing_dir)
            self.fs.write(join(self.processing_dir, HEARTBEAT_NAME), b"", exclusive_create=False)
        except SmbError as e:
            log.warning("could not write the heartbeat in %r: %s", self.processing_dir, e)

    def start_heartbeat(self, stop: threading.Event) -> None:
        """Beat until stop is set, often enough that skew and a missed beat never make us look gone."""
        if not self._takes_over_claims or self._heartbeat_thread is not None:
            return
        interval = min(60.0, self.opts.stale_claim_age / 4)

        def run() -> None:
            while not stop.is_set():
                self.heartbeat()
                stop.wait(interval)

        self._heartbeat_thread = threading.Thread(
            target=run, name=f"heartbeat-{self.instance_id}", daemon=True
        )
        self._heartbeat_thread.start()

    # ------------------------------------------------------------------ selection

    def matches(self, name: str) -> bool:
        if not any(fnmatch.fnmatchcase(name, p) for p in self.opts.file_patterns):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in self._ignore)

    def _ready(self, e: Entry, names: set[str], now: float) -> bool:
        if self.opts.require_marker_pattern:
            if apply_pattern(self.opts.require_marker_pattern, e.name) not in names:
                return False
        if self.opts.stable_for is not None:
            sig = (e.size, e.mtime.timestamp())
            prev = self._seen.get(e.name)
            if prev is None or (prev[0], prev[1]) != sig:
                self._seen[e.name] = (*sig, now)
                return False
            if now - prev[2] < self.opts.stable_for:
                return False
        return True

    # ------------------------------------------------------------------ polling

    def recover(self) -> list[Claimed]:
        """Files left in our processing folder by an earlier run (crash, or the sidecar's
        stream closing before it acked). They were claimed but never completed, so they
        are delivered again: at-least-once."""
        if self.opts.claim_mode is not ClaimMode.RENAME:
            return []
        # Anything waiting for a retry is in the processing folder and is listed below.
        with self._lock:
            self._retry = []
        try:
            entries = self.fs.listdir(self.processing_dir)
        except SmbError as e:
            if e.kind is Kind.NOT_FOUND:
                return []
            raise
        out = [
            Claimed(e.name, join(self.processing_dir, e.name), e.size, e.mtime, recovered=True)
            for e in entries
            if not e.is_dir and e.name != HEARTBEAT_NAME
        ]
        if out:
            log.info(
                "re-delivering %d file(s) claimed by an earlier run from %r", len(out), self.processing_dir
            )
        return out

    def poll(self) -> list[Claimed]:
        now = self._clock()
        if self._takes_over_claims and now - self._last_sweep >= SWEEP_INTERVAL:
            self._last_sweep = now
            self.sweep_stale_claims()
        entries = self.fs.listdir(self.inbound)
        files = [e for e in entries if not e.is_dir]
        names = {e.name for e in files}
        # Forget stability observations for files that went away.
        for gone in set(self._seen) - names:
            del self._seen[gone]

        with self._lock:
            out, self._retry = self._retry, []
        for e in sorted(files, key=lambda x: (x.mtime, x.name)):
            if not self.matches(e.name) or not self._ready(e, names, now):
                continue
            if self.opts.claim_mode is ClaimMode.NONE:
                c = Claimed(e.name, join(self.inbound, e.name), e.size, e.mtime)
                with self._lock:
                    if c.key in self._delivered:
                        continue
                out.append(c)
                continue
            c = self._claim(e)
            if c is not None:
                out.append(c)
        return out

    def sweep_stale_claims(self) -> int:
        """Return files claimed by replicas whose heartbeat is older than staleClaimAge.

        Returns how many files went back to inboundPath. Errors are logged, never raised:
        this must not stop the poll.
        """
        age = self.opts.stale_claim_age
        if age is None:
            return 0
        root = join(self.inbound, self.opts.processing_folder)
        try:
            dirs = [e for e in self.fs.listdir(root) if e.is_dir and e.name != self.instance_id]
        except SmbError as e:
            if e.kind is not Kind.NOT_FOUND:
                log.warning("could not look for stale claims in %r: %s", root, e)
            return 0

        now = self._clock()
        returned = 0
        for d in dirs:
            path = join(root, d.name)
            try:
                entries = self.fs.listdir(path)
            except SmbError as e:
                log.warning("could not look for stale claims in %r: %s", path, e)
                continue
            beat = next((e for e in entries if e.name == HEARTBEAT_NAME), None)
            # No heartbeat (never written, or already cleaned up): the folder's own time is
            # the last sign of life.
            last_seen = (beat or d).mtime.timestamp()
            if now - last_seen < age:
                continue
            files = [e for e in entries if not e.is_dir and e.name != HEARTBEAT_NAME]
            left = 0
            for f in files:
                if self._return_claim(join(path, f.name), f.name):
                    returned += 1
                else:
                    left += 1
            if files:
                log.warning(
                    "replica %r was last seen %.0fs ago: returned %d of its %d claimed file(s) to %r",
                    d.name,
                    now - last_seen,
                    len(files) - left,
                    len(files),
                    self.inbound,
                )
            if not left:
                self._remove_dead_folder(path, beat is not None)
        if returned:
            metrics.CLAIMS_RECOVERED.labels(**self._labels).inc(returned)
        return returned

    def _return_claim(self, src: str, name: str) -> bool:
        try:
            self.fs.rename(src, join(self.inbound, name))
            return True
        except SmbError as e:
            if e.kind is Kind.NOT_FOUND:
                return True  # another replica returned it first
            if e.kind is Kind.EXISTS:
                log.warning("cannot return %r: %r already exists in %r", src, name, self.inbound)
            else:
                log.warning("cannot return %r to %r: %s", src, self.inbound, e)
            return False

    def _remove_dead_folder(self, path: str, has_beat: bool) -> None:
        try:
            if has_beat:
                self.fs.remove(join(path, HEARTBEAT_NAME))
            self.fs.rmdir(path)
        except SmbError as e:
            if e.kind is not Kind.NOT_FOUND:
                log.debug("could not remove the stale processing folder %r: %s", path, e)

    def _claim(self, e: Entry) -> Claimed | None:
        src = join(self.inbound, e.name)
        dst = join(self.processing_dir, e.name)
        self._makedirs(self.processing_dir)
        try:
            self.fs.rename(src, dst)
        except SmbError as err:
            if err.kind is Kind.SHARING_VIOLATION:
                metrics.SHARING_VIOLATIONS.labels(**self._labels).inc()
                log.debug("skipping %r: still open by another client (%s)", src, err.message)
                return None
            if err.kind is Kind.NOT_FOUND:
                log.debug("skipping %r: claimed by another replica", src)
                return None
            if err.kind is Kind.EXISTS:
                log.warning("skipping %r: a file of the same name is still in %r", src, self.processing_dir)
                return None
            raise
        self._seen.pop(e.name, None)
        metrics.FILES_CLAIMED.labels(**self._labels).inc()
        return Claimed(e.name, dst, e.size, e.mtime)

    # ------------------------------------------------------------------ delivery

    def load(self, c: Claimed) -> Message | None:
        """Read a claimed file. None means "skip for now" (still open by someone)."""
        if c.size > self.max_file_size:
            reason = (
                f"{c.size} bytes is over the {self.max_file_size}-byte message limit; raise "
                "--max-message-size together with the sidecar's dapr.io/max-body-size"
            )
            log.error("cannot deliver %r: %s", c.path, reason)
            self.complete(c, ok=False, error=reason)
            return None
        try:
            # Exclusive: fails if anyone else (still) has it open.
            data = self.fs.read(c.path, exclusive=True)
        except SmbError as err:
            if err.kind is Kind.SHARING_VIOLATION:
                metrics.SHARING_VIOLATIONS.labels(**self._labels).inc()
                log.debug("skipping %r: still open by another client, retrying next poll", c.path)
                if self.opts.claim_mode is ClaimMode.RENAME:
                    with self._lock:
                        self._retry.append(c)
                return None
            raise
        md = {
            "fileName": c.name,
            "filePath": c.path,
            "size": str(len(data)),
            "modTime": c.mtime.isoformat().replace("+00:00", "Z"),
        }
        ctype = mimetypes.guess_type(c.name)[0] or "application/octet-stream"
        if self.opts.claim_mode is ClaimMode.NONE:
            with self._lock:
                self._delivered.add(c.key)
        return Message(c, data, md, ctype)

    def complete(self, c: Claimed, *, ok: bool, error: str = "") -> None:
        """Apply afterRead on success, or the error policy on failure."""
        if ok:
            metrics.FILES_PROCESSED.labels(**self._labels).inc()
            self._after_success(c)
        else:
            metrics.FILES_FAILED.labels(**self._labels).inc()
            log.warning("app rejected %r: %s", c.path, error or "no reason given")
            self._after_failure(c)

    def _after_success(self, c: Claimed) -> None:
        mode = self.opts.after_read
        if mode is AfterRead.DELETE:
            self.fs.remove(c.path)
        elif mode is AfterRead.MOVE:
            self._move_to(c, self._folder(self.opts.processed_folder))
        if mode is not AfterRead.NONE:
            self._drop_marker(c)
            with self._lock:
                self._delivered.discard(c.key)

    def _after_failure(self, c: Claimed) -> None:
        if self.opts.error_folder:
            self._move_to(c, self._folder(self.opts.error_folder))
            self._drop_marker(c)
            with self._lock:
                self._delivered.discard(c.key)
        elif self.opts.claim_mode is ClaimMode.RENAME:
            self._put_back(c)
        else:
            with self._lock:
                self._delivered.discard(c.key)

    def _move_to(self, c: Claimed, folder: str) -> str:
        self._makedirs(folder)
        target = join(folder, c.name)
        try:
            self.fs.rename(c.path, target)
            return target
        except SmbError as e:
            if e.kind is not Kind.EXISTS:
                raise
        # Same name processed before: keep both.
        for cand in unique_candidates(c.name):
            target = join(folder, cand)
            try:
                self.fs.rename(c.path, target)
                return target
            except SmbError as e:
                if e.kind is not Kind.EXISTS:
                    raise
        raise SmbError(Kind.EXISTS, "no free name in the destination folder", path=c.path)

    def _put_back(self, c: Claimed) -> None:
        try:
            self.fs.rename(c.path, join(self.inbound, c.name))
        except SmbError as e:
            log.warning("could not return %r to %r for a retry: %s", c.path, self.inbound, e)

    def _drop_marker(self, c: Claimed) -> None:
        if not self.opts.require_marker_pattern:
            return
        marker = join(self.inbound, apply_pattern(self.opts.require_marker_pattern, c.name))
        try:
            self.fs.remove(marker)
        except SmbError as e:
            if e.kind is not Kind.NOT_FOUND:
                log.warning("could not remove completion marker %r: %s", marker, e)
