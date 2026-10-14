"""The error type the binding surfaces to Dapr, and NTSTATUS classification.

An error from Invoke reaches the operator as one flat string in an HTTP 500
body or a gRPC status. That sentence is the whole diagnostic budget, so it
carries the operation, the share-relative path, a credential-free target, the
NTSTATUS and a short explanation.
"""

from __future__ import annotations

import enum
import errno
import socket

# NTSTATUS values we classify. smbprotocol does not define all of them.
STATUS_ACCESS_DENIED = 0xC0000022
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_SHARING_VIOLATION = 0xC0000043
STATUS_DELETE_PENDING = 0xC0000056
STATUS_LOGON_FAILURE = 0xC000006D
STATUS_ACCOUNT_RESTRICTION = 0xC000006E
STATUS_PASSWORD_EXPIRED = 0xC0000071
STATUS_ACCOUNT_DISABLED = 0xC0000072
STATUS_BAD_NETWORK_NAME = 0xC00000CC
STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
STATUS_NOT_A_DIRECTORY = 0xC0000103
STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101
STATUS_USER_SESSION_DELETED = 0xC0000203
STATUS_ACCOUNT_LOCKED_OUT = 0xC0000234
STATUS_NETWORK_SESSION_EXPIRED = 0xC000035C
STATUS_NTLM_BLOCKED = 0xC0000418
STATUS_DISK_FULL = 0xC000007F
STATUS_QUOTA_EXCEEDED = 0xC0000044


class Kind(enum.Enum):
    NOT_FOUND = "not-found"
    EXISTS = "exists"
    SHARING_VIOLATION = "sharing-violation"
    ACCESS_DENIED = "access-denied"
    # The session or transport is gone: reconnect and retry once.
    RECONNECT = "reconnect"
    # Authentication failed: back off, never hammer the KDC (account lockout).
    AUTH = "auth"
    CONFIG = "config"
    OTHER = "other"


_STATUS_KIND = {
    STATUS_OBJECT_NAME_NOT_FOUND: Kind.NOT_FOUND,
    STATUS_OBJECT_PATH_NOT_FOUND: Kind.NOT_FOUND,
    STATUS_OBJECT_NAME_COLLISION: Kind.EXISTS,
    STATUS_SHARING_VIOLATION: Kind.SHARING_VIOLATION,
    STATUS_DELETE_PENDING: Kind.SHARING_VIOLATION,
    STATUS_ACCESS_DENIED: Kind.ACCESS_DENIED,
    STATUS_NETWORK_SESSION_EXPIRED: Kind.RECONNECT,
    STATUS_USER_SESSION_DELETED: Kind.RECONNECT,
    STATUS_LOGON_FAILURE: Kind.AUTH,
    STATUS_ACCOUNT_RESTRICTION: Kind.AUTH,
    STATUS_PASSWORD_EXPIRED: Kind.AUTH,
    STATUS_ACCOUNT_DISABLED: Kind.AUTH,
    STATUS_ACCOUNT_LOCKED_OUT: Kind.AUTH,
    STATUS_NTLM_BLOCKED: Kind.CONFIG,
    STATUS_BAD_NETWORK_NAME: Kind.CONFIG,
}

_EXPLAIN = {
    STATUS_SHARING_VIOLATION: "another client has the file open; it is probably still being written",
    STATUS_DELETE_PENDING: "the file is being deleted by another client",
    STATUS_OBJECT_NAME_COLLISION: 'the target already exists and onTargetExists is "fail"',
    STATUS_ACCESS_DENIED: "the account lacks share or NTFS permission for this operation",
    STATUS_NETWORK_SESSION_EXPIRED: "the Kerberos ticket expired and the server ended the session",
    STATUS_USER_SESSION_DELETED: "the server ended the session",
    STATUS_LOGON_FAILURE: "the server rejected the credentials",
    STATUS_ACCOUNT_LOCKED_OUT: "the account is locked out in AD",
    STATUS_ACCOUNT_DISABLED: "the account is disabled in AD",
    STATUS_PASSWORD_EXPIRED: "the account password has expired",
    STATUS_ACCOUNT_RESTRICTION: "an AD account restriction applies (logon hours, workstations, Protected Users)",
    STATUS_NTLM_BLOCKED: "the server refuses NTLM; use authMode kerberos-password or kerberos-keytab",
    STATUS_BAD_NETWORK_NAME: "the share does not exist on the server",
    STATUS_FILE_IS_A_DIRECTORY: "the path is a directory",
    STATUS_NOT_A_DIRECTORY: "a parent of the path is a file, not a directory",
    STATUS_DIRECTORY_NOT_EMPTY: "the directory is not empty",
    STATUS_DISK_FULL: "the server volume is full",
    STATUS_QUOTA_EXCEEDED: "the account is over its quota",
}


class SmbError(Exception):
    """Everything an operator needs to triage a failed operation."""

    def __init__(
        self,
        kind: Kind,
        message: str,
        *,
        op: str = "",
        path: str = "",
        target: str = "",
        status: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.op = op
        self.path = path
        self.target = target
        self.status = status

    def with_context(self, op: str, path: str, target: str) -> SmbError:
        self.op = self.op or op
        self.path = self.path or path
        self.target = self.target or target
        return self

    def __str__(self) -> str:
        msg = "smb:"
        if self.op:
            msg += f" {self.op}"
        if self.path:
            msg += f' "{self.path}"'
        if self.target:
            msg += f" on {self.target}"
        msg += f": {self.message}"
        if self.status is not None:
            msg += f" [NTSTATUS 0x{self.status:08X}]"
            note = _EXPLAIN.get(self.status)
            if note and note not in self.message:
                msg += f" ({note})"
        return msg


def status_of(exc: BaseException) -> int | None:
    """The NTSTATUS carried by an smbprotocol exception, if any."""
    for attr in ("ntstatus", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and v & 0xC0000000:
            return v
    return None


def classify(exc: BaseException) -> SmbError:
    """Translate any exception from the SMB stack into an SmbError."""
    if isinstance(exc, SmbError):
        return exc

    status = status_of(exc)
    if status is not None:
        kind = _STATUS_KIND.get(status, Kind.OTHER)
        return SmbError(kind, _message(exc), status=status)

    # Imported lazily so the classification can be unit tested without the
    # optional GSSAPI stack.
    try:
        from smbprotocol.exceptions import SMBAuthenticationError, SMBConnectionClosed
    except ImportError:  # pragma: no cover
        SMBAuthenticationError = SMBConnectionClosed = ()  # type: ignore[assignment] # noqa: N806

    if SMBAuthenticationError and isinstance(exc, SMBAuthenticationError):
        return SmbError(Kind.AUTH, _auth_message(exc))
    if SMBConnectionClosed and isinstance(exc, SMBConnectionClosed):
        return SmbError(Kind.RECONNECT, _message(exc) or "the connection was closed")

    name = type(exc).__name__
    module = type(exc).__module__ or ""
    if module.startswith("spnego") or module.startswith("gssapi") or module.startswith("krb5"):
        return SmbError(Kind.AUTH, _auth_message(exc))

    if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError, BrokenPipeError, EOFError)):
        return SmbError(Kind.RECONNECT, f"{name}: {exc}")
    if isinstance(exc, socket.gaierror):
        return SmbError(Kind.CONFIG, f"cannot resolve the server name: {exc}")
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOENT:
            return SmbError(Kind.NOT_FOUND, _message(exc))
        if exc.errno == errno.EEXIST:
            return SmbError(Kind.EXISTS, _message(exc))
        if exc.errno in (
            errno.ECONNRESET,
            errno.ECONNREFUSED,
            errno.EPIPE,
            errno.ETIMEDOUT,
            errno.EHOSTUNREACH,
        ):
            return SmbError(Kind.RECONNECT, f"{name}: {exc}")
    if isinstance(exc, ValueError) and "session" in str(exc).lower():
        return SmbError(Kind.RECONNECT, _message(exc))
    return SmbError(Kind.OTHER, f"{name}: {exc}")


def _message(exc: BaseException) -> str:
    # SMBOSError's str() includes the full UNC path, which we print separately.
    s = getattr(exc, "strerror", None) or str(exc)
    return s.strip() or type(exc).__name__


_AUTH_HINTS = (
    ("clock skew", "the container clock and the domain controller differ by more than 5 minutes"),
    ("cannot find kdc", "no KDC reachable: check kdcs and that TCP/UDP 88 is open to the domain controllers"),
    (
        "cannot contact any kdc",
        "no KDC reachable: check kdcs and that TCP/UDP 88 is open to the domain controllers",
    ),
    (
        "server not found in kerberos database",
        "no SPN cifs/<address>: use the FQDN registered in AD, or setspn",
    ),
    ("preauthentication failed", "wrong password or keytab key for the account"),
    ("password incorrect", "wrong password for the account"),
    ("client not found", "the account does not exist in the realm"),
    ("revoked", "the account is disabled or locked out in AD"),
    ("ticket expired", "the Kerberos ticket expired"),
    ("no credentials", "no Kerberos credentials could be acquired"),
    ("key table entry not found", "the keytab has no key for the principal (check username and enctypes)"),
    ("encryption type", "no common Kerberos encryption type: the account and keytab need AES keys"),
)


def _auth_message(exc: BaseException) -> str:
    s = str(exc).strip() or type(exc).__name__
    low = s.lower()
    for needle, hint in _AUTH_HINTS:
        if needle in low:
            return f"authentication failed: {s} ({hint})"
    return f"authentication failed: {s}"
