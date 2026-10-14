"""The error type the binding surfaces to Dapr, and NTSTATUS classification.

An error from Invoke reaches the operator as one string, so it carries the
operation, the path, a credential-free target and the NTSTATUS.
"""

from __future__ import annotations

import enum
import errno

# NTSTATUS values the binding acts on. smbprotocol does not define all of them.
STATUS_ACCESS_DENIED = 0xC0000022
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_SHARING_VIOLATION = 0xC0000043
STATUS_LOGON_FAILURE = 0xC000006D
STATUS_USER_SESSION_DELETED = 0xC0000203
STATUS_NETWORK_SESSION_EXPIRED = 0xC000035C


class Kind(enum.Enum):
    NOT_FOUND = "not-found"
    EXISTS = "exists"
    SHARING_VIOLATION = "sharing-violation"
    ACCESS_DENIED = "access-denied"
    RECONNECT = "reconnect"  # session or transport gone: reconnect and retry once
    AUTH = "auth"  # authentication failed: back off
    INVALID = "invalid"  # bad request or configuration
    OTHER = "other"


_STATUS_KIND = {
    STATUS_OBJECT_NAME_NOT_FOUND: Kind.NOT_FOUND,
    STATUS_OBJECT_PATH_NOT_FOUND: Kind.NOT_FOUND,
    STATUS_OBJECT_NAME_COLLISION: Kind.EXISTS,
    STATUS_SHARING_VIOLATION: Kind.SHARING_VIOLATION,
    STATUS_ACCESS_DENIED: Kind.ACCESS_DENIED,
    STATUS_NETWORK_SESSION_EXPIRED: Kind.RECONNECT,
    STATUS_USER_SESSION_DELETED: Kind.RECONNECT,
    STATUS_LOGON_FAILURE: Kind.AUTH,
}


class SmbError(Exception):
    def __init__(self, kind: Kind, message: str, *, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.context = ""

    def with_context(self, op: str, path: str, target: str) -> SmbError:
        if not self.context:
            self.context = f"{op}{f' "{path}"' if path else ''} on {target}"
        return self

    def __str__(self) -> str:
        msg = f"smb: {self.context}: {self.message}" if self.context else f"smb: {self.message}"
        if self.status is not None:
            msg += f" [NTSTATUS 0x{self.status:08X}]"
        return msg


def classify(exc: BaseException) -> SmbError:
    """Translate any exception from the SMB stack into an SmbError."""
    if isinstance(exc, SmbError):
        return exc
    status = next(
        (
            v
            for v in (getattr(exc, "ntstatus", None), getattr(exc, "status", None))
            if isinstance(v, int) and v
        ),
        None,
    )
    message = (getattr(exc, "strerror", None) or str(exc)).strip() or type(exc).__name__
    if status is not None:
        return SmbError(_STATUS_KIND.get(status, Kind.OTHER), message, status=status)

    # Imported lazily so classification can be unit tested without the Kerberos stack.
    from smbprotocol.exceptions import SMBAuthenticationError, SMBConnectionClosed

    module = type(exc).__module__ or ""
    if isinstance(exc, SMBAuthenticationError) or module.startswith(("spnego", "gssapi", "krb5")):
        return SmbError(Kind.AUTH, f"authentication failed: {message}")
    if isinstance(exc, (SMBConnectionClosed, ConnectionError, TimeoutError, EOFError)):
        return SmbError(Kind.RECONNECT, message)
    if isinstance(exc, OSError) and exc.errno == errno.ENOENT:
        return SmbError(Kind.NOT_FOUND, message)
    if isinstance(exc, OSError) and exc.errno == errno.EEXIST:
        return SmbError(Kind.EXISTS, message)
    return SmbError(Kind.OTHER, f"{type(exc).__name__}: {message}")
