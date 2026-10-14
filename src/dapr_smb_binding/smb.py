"""The SMB file system on smbprotocol, Kerberos only.

Each component instance has its own connection cache, so instances never share
a session or credentials. smbprotocol does not re-authenticate when a Kerberos
ticket expires and the server ends the session (STATUS_NETWORK_SESSION_EXPIRED),
so every operation reconnects and retries once on that; the new session gets a
fresh ticket.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import kerberos
from .config import AuthMode, Config
from .errors import Kind, SmbError, classify
from .fs import Entry, join

log = logging.getLogger(__name__)

# After a failed login, wait this long before trying again, so retries from the
# poll loop and the app cannot lock the account out in AD.
AUTH_BACKOFF = 60.0


class SmbFileSystem:
    def __init__(
        self, cfg: Config, *, component: str = "default", clock: Callable[[], float] = time.monotonic
    ):
        self.cfg = cfg
        self.component = component
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {}
        self._connected = False
        self._auth_error: SmbError | None = None
        self._auth_retry_at = 0.0
        self._keytab = None

        kerberos.check_gssapi()
        if cfg.realm:
            kerberos.register(component, cfg.realm, cfg.kdcs, cfg.server)
        principal = cfg.username
        if "\\" in principal:  # DOMAIN\user -> user
            principal = principal.split("\\", 1)[1]
        if principal and "@" not in principal and cfg.realm:
            principal = f"{principal}@{cfg.realm}"

        # Passed on EVERY smbclient call, not only register_session: a call without
        # them would open a new session with smbclient's default, "negotiate",
        # which can fall back to NTLM.
        self._kw: dict[str, Any] = {
            "port": cfg.port,
            "encrypt": True if cfg.require_encryption else None,
            "connection_timeout": int(cfg.timeout),
            "connection_cache": self._cache,
            "auth_protocol": "kerberos",
        }
        if cfg.auth_mode is AuthMode.KERBEROS_KEYTAB:
            import spnego

            self._keytab = kerberos.write_keytab(component, cfg.keytab)
            self._kw |= {
                "username": spnego.KerberosKeytab(str(self._keytab), principal or None),
                "password": None,
            }
        else:
            self._kw |= {"username": principal, "password": cfg.password}

    def _ensure_session(self) -> None:
        import smbclient

        with self._lock:
            if self._connected:
                return
            if self._auth_error and self._clock() < self._auth_retry_at:
                wait = self._auth_retry_at - self._clock()
                raise SmbError(
                    Kind.AUTH,
                    f"not retrying login for another {wait:.0f}s, to avoid locking the account out; "
                    f"last error: {self._auth_error.message}",
                )
            try:
                smbclient.register_session(self.cfg.server, **self._kw)
            except Exception as e:
                err = classify(e)
                self._drop_locked()
                if err.kind is Kind.AUTH:
                    self._auth_error, self._auth_retry_at = err, self._clock() + AUTH_BACKOFF
                    log.error(
                        "%s: login to %s failed, retrying in %.0fs: %s",
                        self.component,
                        self.cfg.target,
                        AUTH_BACKOFF,
                        err.message,
                    )
                raise err.with_context("connect", "", self.cfg.target) from e
            self._auth_error = None
            self._connected = True

    def _drop_locked(self) -> None:
        import smbclient

        # Closing a dead session can itself raise; ignore that and start afresh.
        try:
            smbclient.reset_connection_cache(fail_on_error=False, connection_cache=self._cache)
        except Exception as e:  # noqa: BLE001
            log.debug("ignoring error while dropping the connection: %s", e)
        self._cache.clear()
        self._connected = False

    def close(self) -> None:
        with self._lock:
            self._drop_locked()
        kerberos.unregister(self.component)
        if self._keytab:
            self._keytab.unlink(missing_ok=True)

    def _call[T](self, op: str, path: str, fn: Callable[[], T]) -> T:
        """Run fn on a live session; reconnect and retry once if the session expired."""
        for attempt in (1, 2):
            self._ensure_session()
            try:
                return fn()
            except Exception as e:
                err = classify(e)
                if err.kind is Kind.RECONNECT:
                    with self._lock:
                        self._drop_locked()
                    if attempt == 1:
                        log.info("%s: %s; reconnecting", self.component, err.message)
                        continue
                raise err.with_context(op, path, self.cfg.target) from e
        raise AssertionError("unreachable")

    def _unc(self, path: str) -> str:
        return "\\".join(["", "", self.cfg.server, self.cfg.share, *join(path).split("/")]).rstrip("\\")

    def listdir(self, path: str) -> list[Entry]:
        import smbclient

        def fn() -> list[Entry]:
            out = []
            for e in smbclient.scandir(self._unc(path), **self._kw):
                info = e.smb_info
                mtime = info.last_write_time
                out.append(
                    Entry(
                        e.name,
                        e.is_dir(),
                        info.end_of_file,
                        mtime if mtime.tzinfo else mtime.replace(tzinfo=dt.UTC),
                    )
                )
            return out

        return self._call("list", path, fn)

    def read(self, path: str, *, exclusive: bool = False) -> bytes:
        import smbclient

        def fn() -> bytes:
            share = None if exclusive else "r"
            with smbclient.open_file(self._unc(path), mode="rb", share_access=share, **self._kw) as f:
                return f.read()

        return self._call("get", path, fn)

    def write_new(self, path: str, data: bytes) -> None:
        import smbclient

        def fn() -> None:
            with smbclient.open_file(self._unc(path), mode="xb", share_access=None, **self._kw) as f:
                f.write(data)

        self._call("create", path, fn)

    def rename(self, src: str, dst: str) -> None:
        import smbclient

        self._call("rename", src, lambda: smbclient.rename(self._unc(src), self._unc(dst), **self._kw))

    def remove(self, path: str) -> None:
        import smbclient

        self._call("delete", path, lambda: smbclient.remove(self._unc(path), **self._kw))

    def makedirs(self, path: str) -> None:
        import smbclient

        if join(path):
            self._call("mkdir", path, lambda: smbclient.makedirs(self._unc(path), exist_ok=True, **self._kw))

    def ping(self) -> None:
        """A stat of the share root proves the session and the tree connect."""
        import smbclient

        self._call("ping", "", lambda: smbclient.stat(self._unc(""), **self._kw))
