"""The SMB file system, on smbprotocol, with Kerberos-only auth by default.

Each component instance has its own connection cache, so instances never
share a session or credentials. Session setup gets a fresh Kerberos ticket
(pyspnego does the kinit into a private MEMORY ccache), which is what makes
reconnecting after STATUS_NETWORK_SESSION_EXPIRED work: smbprotocol does not
re-authenticate an expired session on its own, so we drop the connection and
set up a new one.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import kerberos, metrics
from .config import AuthMode, Config
from .errors import Kind, SmbError, classify
from .fs import Entry, join

log = logging.getLogger(__name__)

# Backoff after failed authentication, so a wrong password cannot lock the
# account out in AD through retries from the poll loop or the app.
AUTH_BACKOFF_INITIAL = 15.0
AUTH_BACKOFF_MAX = 15 * 60.0


class SmbFileSystem:
    def __init__(self, cfg: Config, *, component: str = "", clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.component = component or "default"
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {}
        self._connected = False
        self._auth_failures = 0
        self._auth_next_attempt = 0.0
        self._auth_last_error: SmbError | None = None
        self._keytab_path = None
        self._labels = {"component": self.component, "target": cfg.target}

        if cfg.auth_mode is not AuthMode.NTLM:
            kerberos.check_gssapi()
            if cfg.realm:
                kerberos.KRB5_CONFIG.register(
                    self.component, kerberos.RealmSpec(realm=cfg.realm, kdcs=cfg.kdcs, hosts=(cfg.server,))
                )
        if cfg.auth_mode is AuthMode.KERBEROS_KEYTAB:
            self._keytab_path = kerberos.write_keytab(self.component, cfg.keytab)

        self._kwargs = self._session_kwargs()

    # ------------------------------------------------------------------ session

    def _principal(self) -> str:
        u = self.cfg.username
        if "\\" in u:  # DOMAIN\user -> user@REALM
            u = u.split("\\", 1)[1]
        if u and "@" not in u and self.cfg.realm:
            u = f"{u}@{self.cfg.realm}"
        return u

    def _session_kwargs(self) -> dict[str, Any]:
        cfg = self.cfg
        kw: dict[str, Any] = {
            "port": cfg.port,
            "encrypt": True if cfg.require_encryption else None,
            "connection_timeout": int(max(1, cfg.timeout)),
            "connection_cache": self._cache,
        }
        if cfg.auth_mode is AuthMode.KERBEROS_PASSWORD:
            kw.update(username=self._principal(), password=cfg.password, auth_protocol="kerberos")
        elif cfg.auth_mode is AuthMode.KERBEROS_KEYTAB:
            import spnego

            kw.update(
                username=spnego.KerberosKeytab(
                    keytab=str(self._keytab_path), principal=self._principal() or None
                ),
                password=None,
                auth_protocol="kerberos",
            )
        else:
            kw.update(username=cfg.username, password=cfg.password, auth_protocol="ntlm")
        return kw

    def _ensure_session(self) -> None:
        with self._lock:
            if self._connected:
                return
            now = self._clock()
            if self._auth_failures and now < self._auth_next_attempt:
                wait = self._auth_next_attempt - now
                last = self._auth_last_error.message if self._auth_last_error else "unknown"
                raise SmbError(
                    Kind.AUTH,
                    f"not retrying authentication for another {wait:.0f}s after {self._auth_failures} "
                    f"failure(s), to avoid locking the account out; last error: {last}",
                )
            import smbclient

            try:
                smbclient.register_session(self.cfg.server, **self._kwargs)
            except Exception as e:
                err = classify(e)
                self._drop_locked()
                if err.kind is Kind.AUTH or (err.kind is Kind.CONFIG and err.status is not None):
                    self._auth_failures += 1
                    delay = min(AUTH_BACKOFF_INITIAL * 2 ** (self._auth_failures - 1), AUTH_BACKOFF_MAX)
                    self._auth_next_attempt = now + delay
                    self._auth_last_error = err
                    metrics.AUTH_FAILURES.labels(**self._labels).inc()
                    log.error(
                        "authentication to %s failed (%d in a row), next attempt in %.0fs: %s",
                        self.cfg.target,
                        self._auth_failures,
                        delay,
                        err.message,
                        extra={"component": self.component, "target": self.cfg.target},
                    )
                raise err.with_context("connect", "", self.cfg.target) from e
            if self._auth_failures:
                log.info(
                    "authentication to %s succeeded after %d failure(s)", self.cfg.target, self._auth_failures
                )
            self._auth_failures = 0
            self._auth_last_error = None
            self._connected = True

    def _drop_locked(self) -> None:
        """Throw the connection away. Closing a dead session can itself raise, so ignore that."""
        import smbclient

        try:
            smbclient.reset_connection_cache(fail_on_error=False, connection_cache=self._cache)
        except Exception as e:  # noqa: BLE001
            log.debug("ignoring error while dropping the connection to %s: %s", self.cfg.target, e)
        self._cache.clear()
        self._connected = False

    def drop(self) -> None:
        with self._lock:
            self._drop_locked()

    def close(self) -> None:
        self.drop()
        kerberos.KRB5_CONFIG.unregister(self.component)
        kerberos.remove_keytab(self._keytab_path)

    def _call[T](
        self,
        op: str,
        path: str,
        fn: Callable[[], T],
        *,
        settle: Callable[[SmbError], bool] | None = None,
    ) -> T | None:
        """Run fn on a live session. Reconnect and retry once if the session expired or the
        connection dropped. fn must be safe to run twice.

        A connection can drop after the server carried out the request but before the reply
        arrived. The retry then fails because the work is already done (a rename finds its
        source gone). settle decides, for an error on the retry only, whether that is the
        case; if it returns True the operation counts as done.
        """
        for attempt in (0, 1):
            self._ensure_session()
            try:
                return fn()
            except Exception as e:
                err = classify(e)
                if (
                    attempt == 1
                    and settle is not None
                    and err.kind is not Kind.RECONNECT
                    and self._settled(settle, err)
                ):
                    log.info(
                        "%s %r on %s: the attempt before the reconnect had already completed (%s)",
                        op,
                        path,
                        self.cfg.target,
                        err.message,
                        extra={"component": self.component, "target": self.cfg.target},
                    )
                    return None
                if err.kind is Kind.RECONNECT and attempt == 0:
                    log.info(
                        "%s %r on %s: %s; reconnecting and retrying once",
                        op,
                        path,
                        self.cfg.target,
                        err.message,
                        extra={"component": self.component, "target": self.cfg.target},
                    )
                    metrics.RECONNECTS.labels(**self._labels).inc()
                    self.drop()
                    continue
                if err.kind is Kind.RECONNECT:
                    self.drop()
                raise err.with_context(op, path, self.cfg.target) from e
        raise AssertionError("unreachable")  # pragma: no cover

    def _settled(self, settle: Callable[[SmbError], bool], err: SmbError) -> bool:
        try:
            return settle(err)
        except Exception as e:  # noqa: BLE001 - cannot prove it ran, so report the original error
            log.debug("could not check whether a retried operation had completed: %s", e)
            return False

    def _unc(self, path: str) -> str:
        rel = join(path).replace("/", "\\")
        base = f"\\\\{self.cfg.server}\\{self.cfg.share}"
        return f"{base}\\{rel}" if rel else base

    # ------------------------------------------------------------------ FileSystem

    def listdir(self, path: str) -> list[Entry]:
        import smbclient

        def fn() -> list[Entry]:
            out = []
            for e in smbclient.scandir(self._unc(path), **self._kwargs):
                if e.name in (".", ".."):
                    continue
                info = e.smb_info
                out.append(
                    Entry(
                        name=e.name,
                        is_dir=e.is_dir(),
                        size=info.end_of_file,
                        mtime=_utc(info.last_write_time),
                    )
                )
            return out

        return self._call("list", path, fn)

    def stat(self, path: str) -> Entry:
        import stat as st

        import smbclient

        def fn() -> Entry:
            r = smbclient.stat(self._unc(path), **self._kwargs)
            name = path.rsplit("/", 1)[-1]
            return Entry(
                name=name,
                is_dir=st.S_ISDIR(r.st_mode),
                size=r.st_size,
                mtime=dt.datetime.fromtimestamp(r.st_mtime, tz=dt.UTC),
            )

        return self._call("stat", path, fn)

    def read(self, path: str, *, exclusive: bool = False) -> bytes:
        import smbclient

        def fn() -> bytes:
            # share_access=None denies all sharing: fails if anyone else has it open.
            share = None if exclusive else "r"
            with smbclient.open_file(self._unc(path), mode="rb", share_access=share, **self._kwargs) as f:
                return f.read()

        return self._call("get", path, fn)

    def write(self, path: str, data: bytes, *, exclusive_create: bool) -> None:
        import smbclient

        # Set once the server has created the file for us. A retry after that must
        # overwrite our own (possibly partial) file, not fail because it exists.
        created = False

        def fn() -> None:
            nonlocal created
            mode = "xb" if exclusive_create and not created else "wb"
            share = None if exclusive_create else "r"
            with smbclient.open_file(self._unc(path), mode=mode, share_access=share, **self._kwargs) as f:
                created = True
                f.write(data)

        self._call("create", path, fn)

    def rename(self, src: str, dst: str) -> None:
        import smbclient

        self._call(
            "rename",
            src,
            lambda: smbclient.rename(self._unc(src), self._unc(dst), **self._kwargs),
            settle=lambda err: self._rename_done(err, src, dst),
        )

    def replace(self, src: str, dst: str) -> None:
        import smbclient

        self._call(
            "rename",
            src,
            lambda: smbclient.replace(self._unc(src), self._unc(dst), **self._kwargs),
            settle=lambda err: self._rename_done(err, src, dst),
        )

    def remove(self, path: str) -> None:
        import smbclient

        # The goal of a delete is that the file is gone; if the retry finds it gone, it is.
        self._call(
            "delete",
            path,
            lambda: smbclient.remove(self._unc(path), **self._kwargs),
            settle=lambda err: err.kind is Kind.NOT_FOUND,
        )

    def _rename_done(self, err: SmbError, src: str, dst: str) -> bool:
        """After a reconnect: the source is gone and the target exists, so our rename ran.

        For rename this is conclusive: had the target existed before, the first attempt
        would have failed with EXISTS rather than a dropped connection. For replace it is
        the best evidence available.
        """
        return err.kind is Kind.NOT_FOUND and not self._exists(src) and self._exists(dst)

    def _exists(self, path: str) -> bool:
        import smbclient

        try:
            smbclient.stat(self._unc(path), **self._kwargs)
            return True
        except Exception as e:
            if classify(e).kind is Kind.NOT_FOUND:
                return False
            raise

    def makedirs(self, path: str) -> None:
        import smbclient

        if not join(path):
            return
        self._call("mkdir", path, lambda: smbclient.makedirs(self._unc(path), exist_ok=True, **self._kwargs))

    def rmdir(self, path: str) -> None:
        import smbclient

        self._call(
            "rmdir",
            path,
            lambda: smbclient.rmdir(self._unc(path), **self._kwargs),
            settle=lambda err: err.kind is Kind.NOT_FOUND,
        )

    def ping(self) -> None:
        import smbclient

        # A stat of the share root proves the session and the tree connect.
        self._call("ping", "", lambda: smbclient.stat(self._unc(""), **self._kwargs))


def _utc(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)
