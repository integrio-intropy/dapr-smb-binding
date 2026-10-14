"""Parse and validate the component metadata.

Field names follow Dapr's SFTP binding wherever it has an equivalent
(``address``, ``username``, ``password``, ``rootPath``); everything else is an
extension shared with the other Intropy file bindings. See CONTRACT.md.
"""

from __future__ import annotations

import base64
import binascii
import enum
import ipaddress
import re
from dataclasses import dataclass, field

DEFAULT_PORT = 445
DEFAULT_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 10.0
DEFAULT_TEMP_FILE_NAME_PATTERN = "{name}.tmp"
DEFAULT_TEMP_FOLDER = "_staging"
DEFAULT_PROCESSING_FOLDER = "processing"
DEFAULT_PROCESSED_FOLDER = "processed"
DEFAULT_ERROR_FOLDER = "error"
DEFAULT_STALE_CLAIM_AGE = 15 * 60.0
# The age is the server's mtime against our clock; Kerberos allows 5 minutes of skew.
MIN_SAFE_STALE_TEMP_FILE_AGE = 600.0


class ConfigError(ValueError):
    """The component metadata is invalid. The message is shown to the operator verbatim."""


class AuthMode(enum.StrEnum):
    KERBEROS_PASSWORD = "kerberos-password"  # noqa: S105 - a mode name, not a password
    KERBEROS_KEYTAB = "kerberos-keytab"
    # Explicit per-share opt-in only. Never a fallback.
    NTLM = "ntlm"


class TempFileMode(enum.StrEnum):
    NONE = "none"
    NAME = "name"
    FOLDER = "folder"


class OnTargetExists(enum.StrEnum):
    FAIL = "fail"
    OVERWRITE = "overwrite"
    UNIQUE_NAME = "uniqueName"


class ClaimMode(enum.StrEnum):
    RENAME = "rename"
    NONE = "none"


class AfterRead(enum.StrEnum):
    MOVE = "move"
    DELETE = "delete"
    NONE = "none"


@dataclass(frozen=True)
class WriteOptions:
    temp_file_mode: TempFileMode = TempFileMode.NAME
    temp_file_name_pattern: str = DEFAULT_TEMP_FILE_NAME_PATTERN
    temp_folder: str = DEFAULT_TEMP_FOLDER
    on_target_exists: OnTargetExists = OnTargetExists.FAIL
    stale_temp_file_age: float | None = None
    completion_marker_pattern: str = ""


@dataclass(frozen=True)
class ReadOptions:
    inbound_path: str = ""
    file_patterns: tuple[str, ...] = ("*",)
    poll_interval: float = DEFAULT_POLL_INTERVAL
    ignore_patterns: tuple[str, ...] = ()
    claim_mode: ClaimMode = ClaimMode.RENAME
    processing_folder: str = DEFAULT_PROCESSING_FOLDER
    require_marker_pattern: str = ""
    stable_for: float | None = None
    after_read: AfterRead = AfterRead.MOVE
    processed_folder: str = DEFAULT_PROCESSED_FOLDER
    error_folder: str = DEFAULT_ERROR_FOLDER
    # Return claims of a replica whose heartbeat is older than this. None = never.
    stale_claim_age: float | None = DEFAULT_STALE_CLAIM_AGE


@dataclass(frozen=True)
class Config:
    server: str
    port: int
    share: str
    root_path: str
    auth_mode: AuthMode
    username: str = ""
    password: str = field(default="", repr=False)
    keytab: bytes = field(default=b"", repr=False)
    realm: str = ""
    kdcs: tuple[str, ...] = ()
    require_encryption: bool = True
    timeout: float = DEFAULT_TIMEOUT
    write: WriteOptions = field(default_factory=WriteOptions)
    read: ReadOptions = field(default_factory=ReadOptions)

    @property
    def target(self) -> str:
        """A credential-free description for logs and error messages."""
        port = "" if self.port == DEFAULT_PORT else f":{self.port}"
        return f"smb://{self.server}{port}/{self.share}"

    def __str__(self) -> str:
        # Never print secrets, even by accident through %s or an f-string.
        return (
            f"smb.Config(target={self.target} rootPath={self.root_path!r} authMode={self.auth_mode} "
            f"username={self.username!r} password={_redacted(self.password)} "
            f"keytab={_redacted(self.keytab)} realm={self.realm!r} kdcs={list(self.kdcs)} "
            f"requireEncryption={self.require_encryption} timeout={self.timeout}s)"
        )

    __repr__ = __str__


def _redacted(v: str | bytes) -> str:
    return "<redacted>" if v else "<unset>"


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(name: str, value: str) -> float | None:
    """Parse a Go-style duration ("10s", "1h30m", "500ms") into seconds.

    Empty means unset (None). A bare number is rejected rather than guessed at:
    a silent 1000x error on a timeout is a miserable bug to chase.
    """
    s = value.strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        raise ConfigError(f'{name} "{value}" needs a unit, for example "{s}s"')
    pos, total = 0, 0.0
    for m in _DURATION_PART.finditer(s):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s) or pos == 0:
        raise ConfigError(f'{name} "{value}" is not a valid duration, for example "10s", "5m" or "1h"')
    return total


def parse_bool(name: str, value: str, default: bool) -> bool:
    s = value.strip().lower()
    if not s:
        return default
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f'{name} "{value}" is not a boolean: use "true" or "false"')


def parse_list(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(",") if p.strip())


def _enum[E: enum.StrEnum](cls: type[E], name: str, value: str, default: E) -> E:
    s = value.strip()
    if not s:
        return default
    for member in cls:
        if member.value.lower() == s.lower():
            return member
    allowed = ", ".join(f'"{m.value}"' for m in cls)
    raise ConfigError(f'{name} "{value}" is not valid: use {allowed}')


def normalize_path(p: str) -> str:
    """Normalise a share-relative path to forward slashes, no leading or trailing slash.

    Not a confinement: like the SFTP binding, ".." is passed through and the
    server's share and NTFS permissions decide what the account may reach.
    """
    parts = [x for x in p.replace("\\", "/").split("/") if x and x != "."]
    return "/".join(parts)


def _check_pattern(name: str, value: str) -> str:
    if value and "{name}" not in value:
        raise ConfigError(
            f'{name} "{value}" must contain the placeholder {{name}}, for example "{{name}}.tmp"'
        )
    return value


class _Props:
    """Case-insensitive view of the metadata, as Dapr's DecodeMetadata gives built-in components."""

    def __init__(self, props: dict[str, str]):
        self._p = {k.lower(): v for k, v in props.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._p.get(key.lower(), default)

    def has(self, key: str) -> bool:
        """Whether the key is present at all, even with an empty value."""
        return key.lower() in self._p


def parse(props: dict[str, str]) -> tuple[Config, list[str]]:
    """Decode, validate and default the component metadata.

    Returns the config and a list of warnings for Init to log without failing.
    """
    p = _Props(props)
    warns: list[str] = []

    server, port = _parse_address(p.get("address"))

    share = p.get("share").strip().strip("\\/")
    if not share:
        raise ConfigError('share is required: the name of the SMB share, for example "exchange"')
    if "/" in share or "\\" in share:
        raise ConfigError(
            f'share "{share}" must be the share name only; put the folder inside the share in rootPath'
        )

    raw_root = p.get("rootPath")
    if not raw_root.strip():
        raise ConfigError(
            'rootPath is required: the folder inside the share this binding works in, for example "/" or "in/from-intropy"'
        )
    root_path = normalize_path(raw_root)

    auth_mode = _enum(AuthMode, "authMode", p.get("authMode"), AuthMode.KERBEROS_PASSWORD)
    username = p.get("username").strip()
    password = p.get("password")
    keytab = _parse_keytab(p.get("keytab"))

    if auth_mode in (AuthMode.KERBEROS_PASSWORD, AuthMode.NTLM):
        if not username:
            raise ConfigError(f'username is required when authMode is "{auth_mode}"')
        if not password:
            raise ConfigError(f'password is required when authMode is "{auth_mode}"')
        if keytab:
            warns.append(f'keytab is set but ignored because authMode is "{auth_mode}"')
    if auth_mode is AuthMode.KERBEROS_KEYTAB:
        if not keytab:
            raise ConfigError(
                'keytab is required when authMode is "kerberos-keytab" (base64 of the keytab file)'
            )
        if password:
            warns.append('password is set but ignored because authMode is "kerberos-keytab"')

    realm = p.get("realm").strip().upper()
    if not realm and "@" in username:
        realm = username.rsplit("@", 1)[1].upper()
    kdcs = parse_list(p.get("kdcs"))

    if auth_mode is not AuthMode.NTLM:
        _require_hostname(server)
        if kdcs and not realm:
            raise ConfigError("realm is required when kdcs is set, or give username as user@REALM")
    else:
        warns.append(
            f'authMode is "ntlm": NTLM is deprecated and many domains block it; '
            f"prefer kerberos-password or kerberos-keytab for {server}"
        )
        if kdcs or realm:
            warns.append('realm and kdcs are ignored because authMode is "ntlm"')

    require_encryption = parse_bool("requireEncryption", p.get("requireEncryption"), True)
    if not require_encryption:
        warns.append(
            f"requireEncryption is false: file contents to {server} are only encrypted if the server asks for it"
        )

    timeout = parse_duration("timeout", p.get("timeout"))
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if timeout <= 0:
        raise ConfigError("timeout must be positive")

    write = _parse_write(p)
    read = _parse_read(p, root_path, write)
    stale = write.stale_temp_file_age
    if stale is not None and stale < MIN_SAFE_STALE_TEMP_FILE_AGE:
        warns.append(
            f"staleTempFileAge is {p.get('staleTempFileAge')}: below 10m a slow upload's temp file, or clock "
            f"skew between this container and {server}, can make a live temp file look stale and get it deleted"
        )
    claim_age = read.stale_claim_age
    if claim_age is not None and claim_age < MIN_SAFE_STALE_TEMP_FILE_AGE:
        warns.append(
            f"staleClaimAge is {p.get('staleClaimAge')}: below 10m, clock skew between this container and "
            f"{server} can make a live replica look gone, and its files would be delivered twice"
        )

    cfg = Config(
        server=server,
        port=port,
        share=share,
        root_path=root_path,
        auth_mode=auth_mode,
        username=username,
        password=password,
        keytab=keytab,
        realm=realm,
        kdcs=kdcs,
        require_encryption=require_encryption,
        timeout=timeout,
        write=write,
        read=read,
    )
    return cfg, warns


def _parse_address(addr: str) -> tuple[str, int]:
    a = addr.strip()
    if not a:
        raise ConfigError(
            'address is required: the file server FQDN as registered in AD, for example "fs01.corp.example.com"'
        )
    if a.startswith("\\\\") or a.startswith("//") or "://" in a:
        raise ConfigError(f'address "{addr}" must be a host name or host:port, not a URL or UNC path')
    host, port = a, DEFAULT_PORT
    if a.startswith("["):  # [v6]:port
        m = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", a)
        if not m:
            raise ConfigError(f'address "{addr}" is not a valid host:port')
        host, port = m.group(1), int(m.group(2) or DEFAULT_PORT)
    elif a.count(":") == 1:
        host, ps = a.split(":")
        if not ps.isdigit():
            raise ConfigError(f'address "{addr}": port "{ps}" is not a number')
        port = int(ps)
    if not host:
        raise ConfigError(f'address "{addr}" is missing a host')
    if not 1 <= port <= 65535:
        raise ConfigError(f"address port {port} is out of range 1-65535")
    return host.lower(), port


def _require_hostname(server: str) -> None:
    try:
        ipaddress.ip_address(server)
    except ValueError:
        return
    raise ConfigError(
        f'address "{server}" is an IP address. Kerberos needs the name the server is registered under in AD '
        f"(SPN cifs/<fqdn>). Use the FQDN, and map it with pod hostAliases if the customer's DNS is not reachable."
    )


def _parse_keytab(v: str) -> bytes:
    s = "".join(v.split())
    if not s:
        return b""
    try:
        data = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ConfigError("keytab must be the keytab file encoded as base64") from e
    # MIT keytab files start with 0x05 0x01 or 0x05 0x02.
    if len(data) < 2 or data[0] != 0x05 or data[1] not in (0x01, 0x02):
        raise ConfigError("keytab does not look like a keytab file after base64 decoding")
    return data


def _parse_write(p: _Props) -> WriteOptions:
    mode = _enum(TempFileMode, "tempFileMode", p.get("tempFileMode"), TempFileMode.NAME)
    pattern = _check_pattern(
        "tempFileNamePattern", p.get("tempFileNamePattern").strip() or DEFAULT_TEMP_FILE_NAME_PATTERN
    )
    if pattern.replace("{name}", "") == "" or "/" in pattern or "\\" in pattern:
        raise ConfigError(
            f'tempFileNamePattern "{pattern}" must add something to the name and must not contain a path'
        )
    temp_folder = normalize_path(p.get("tempFolder")) if p.get("tempFolder").strip() else DEFAULT_TEMP_FOLDER
    if mode is TempFileMode.FOLDER and not temp_folder:
        raise ConfigError('tempFolder must not be empty when tempFileMode is "folder"')
    marker = _check_pattern("completionMarkerPattern", p.get("completionMarkerPattern").strip())
    if marker == "{name}":
        raise ConfigError("completionMarkerPattern must differ from the file name itself")
    stale = parse_duration("staleTempFileAge", p.get("staleTempFileAge"))
    if stale is not None and stale <= 0:
        raise ConfigError("staleTempFileAge must be positive, or empty to disable cleanup")
    return WriteOptions(
        temp_file_mode=mode,
        temp_file_name_pattern=pattern,
        temp_folder=temp_folder,
        on_target_exists=_enum(
            OnTargetExists, "onTargetExists", p.get("onTargetExists"), OnTargetExists.FAIL
        ),
        stale_temp_file_age=stale,
        completion_marker_pattern=marker,
    )


def _parse_read(p: _Props, root_path: str, write: WriteOptions) -> ReadOptions:
    inbound = p.get("inboundPath")
    inbound_path = normalize_path(inbound) if inbound.strip() else root_path

    poll = parse_duration("pollInterval", p.get("pollInterval"))
    if poll is None:
        poll = DEFAULT_POLL_INTERVAL
    if poll < 0.1:
        raise ConfigError("pollInterval must be at least 100ms")

    stable_for = parse_duration("stableFor", p.get("stableFor"))
    if stable_for is not None and stable_for <= 0:
        raise ConfigError("stableFor must be positive, or empty to disable the stability check")

    def folder(key: str, default: str) -> str:
        v = p.get(key)
        return normalize_path(v) if v.strip() else default

    after_read = _enum(AfterRead, "afterRead", p.get("afterRead"), AfterRead.MOVE)
    processed = folder("processedFolder", DEFAULT_PROCESSED_FOLDER)
    if after_read is AfterRead.MOVE and not processed:
        raise ConfigError('processedFolder must not be empty when afterRead is "move"')

    # An explicitly empty errorFolder means "put failed files back for another try".
    error_folder = normalize_path(p.get("errorFolder")) if p.has("errorFolder") else DEFAULT_ERROR_FOLDER

    # Like errorFolder: unset is the default, explicitly empty switches it off.
    stale_claim_age = parse_duration("staleClaimAge", p.get("staleClaimAge"))
    if not p.has("staleClaimAge"):
        stale_claim_age = DEFAULT_STALE_CLAIM_AGE
    elif stale_claim_age is not None and stale_claim_age <= 0:
        raise ConfigError(
            "staleClaimAge must be positive, or empty to never take over another replica's claims"
        )

    return ReadOptions(
        inbound_path=inbound_path,
        file_patterns=parse_list(p.get("filePattern")) or ("*",),
        poll_interval=poll,
        ignore_patterns=parse_list(p.get("ignorePatterns")),
        claim_mode=_enum(ClaimMode, "claimMode", p.get("claimMode"), ClaimMode.RENAME),
        processing_folder=folder("processingFolder", DEFAULT_PROCESSING_FOLDER),
        require_marker_pattern=_check_pattern("requireMarkerPattern", p.get("requireMarkerPattern").strip()),
        stable_for=stable_for,
        after_read=after_read,
        processed_folder=processed,
        error_folder=error_folder,
        stale_claim_age=stale_claim_age,
    )
