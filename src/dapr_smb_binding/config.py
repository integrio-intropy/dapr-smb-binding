"""Parse and validate the component metadata.

Field names follow Dapr's SFTP binding where it has an equivalent (address,
username, password, rootPath). See CONTRACT.md.
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


class ConfigError(ValueError):
    """The component metadata is invalid. The message is shown to the operator verbatim."""


class AuthMode(enum.StrEnum):
    KERBEROS_PASSWORD = "kerberos-password"  # noqa: S105 - a mode name, not a password
    KERBEROS_KEYTAB = "kerberos-keytab"


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
    # Write side: files are written under this name, then renamed into place.
    temp_file_name_pattern: str = DEFAULT_TEMP_FILE_NAME_PATTERN
    # Read side.
    inbound_path: str = ""
    file_patterns: tuple[str, ...] = ("*",)
    ignore_patterns: tuple[str, ...] = ()
    poll_interval: float = DEFAULT_POLL_INTERVAL
    processed_folder: str = "processed"
    error_folder: str = "error"

    @property
    def target(self) -> str:
        """A credential-free description for logs and error messages."""
        port = "" if self.port == DEFAULT_PORT else f":{self.port}"
        return f"smb://{self.server}{port}/{self.share}"

    def __str__(self) -> str:
        # Never print secrets, even by accident through %s or an f-string.
        return (
            f"smb.Config(target={self.target} rootPath={self.root_path!r} authMode={self.auth_mode} "
            f"username={self.username!r} password={_redacted(self.password)} keytab={_redacted(self.keytab)} "
            f"realm={self.realm!r} kdcs={list(self.kdcs)})"
        )

    __repr__ = __str__


def _redacted(v: str | bytes) -> str:
    return "<redacted>" if v else "<unset>"


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_DURATION_UNITS = {"ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(name: str, value: str, default: float) -> float:
    """Parse a Go-style duration ("10s", "1m30s", "500ms") into seconds.

    A bare number is rejected rather than guessed at: a silent 1000x error on a
    timeout is a miserable bug to chase.
    """
    s = value.strip()
    if not s:
        return default
    if re.fullmatch(r"\d+(\.\d+)?", s):
        raise ConfigError(f'{name} "{value}" needs a unit, for example "{s}s"')
    pos, total = 0, 0.0
    for m in _DURATION_PART.finditer(s):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2)]
        pos = m.end()
    if pos != len(s) or total <= 0:
        raise ConfigError(f'{name} "{value}" is not a valid positive duration, for example "10s" or "5m"')
    return total


def normalize_path(p: str) -> str:
    """Share-relative, forward slashes, no leading or trailing slash.

    Not a confinement: like the SFTP binding, ".." is passed through and the
    share's permissions decide what the account may reach.
    """
    return "/".join(x for x in p.replace("\\", "/").split("/") if x and x != ".")


def _list(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(",") if p.strip())


def parse(props: dict[str, str]) -> tuple[Config, list[str]]:
    """Decode, validate and default the component metadata.

    Keys are case-insensitive, as Dapr's DecodeMetadata makes them for built-in
    components. Returns the config and warnings for Init to log.
    """
    p = {k.lower(): v for k, v in props.items()}

    def get(key: str) -> str:
        return p.get(key.lower(), "").strip()

    warns: list[str] = []
    server, port = _parse_address(get("address"))

    share = get("share").strip("\\/")
    if not share or "/" in share or "\\" in share:
        raise ConfigError('share is required and must be the share name only, for example "exchange"')

    if not get("rootPath"):
        raise ConfigError(
            'rootPath is required: the folder inside the share, for example "/" or "in/from-intropy"'
        )
    root_path = normalize_path(get("rootPath"))

    mode_s = get("authMode") or AuthMode.KERBEROS_PASSWORD.value
    try:
        auth_mode = AuthMode(mode_s.lower())
    except ValueError:
        raise ConfigError(
            f'authMode "{mode_s}" is not valid: use "kerberos-password" or "kerberos-keytab"'
        ) from None

    username, password = get("username"), p.get("password", "")
    keytab = _parse_keytab(get("keytab"))
    if auth_mode is AuthMode.KERBEROS_PASSWORD and not (username and password):
        raise ConfigError('username and password are required when authMode is "kerberos-password"')
    if auth_mode is AuthMode.KERBEROS_KEYTAB and not keytab:
        raise ConfigError('keytab is required when authMode is "kerberos-keytab" (base64 of the keytab file)')

    realm = get("realm").upper()
    if not realm and "@" in username:
        realm = username.rsplit("@", 1)[1].upper()
    kdcs = _list(get("kdcs"))
    if kdcs and not realm:
        raise ConfigError("realm is required when kdcs is set, or give username as user@REALM")

    require_encryption = get("requireEncryption").lower() not in ("false", "0", "no")
    if not require_encryption:
        warns.append(
            f"requireEncryption is false: file contents to {server} are only encrypted if the server asks"
        )

    pattern = get("tempFileNamePattern") or DEFAULT_TEMP_FILE_NAME_PATTERN
    if "{name}" not in pattern or pattern == "{name}" or "/" in pattern or "\\" in pattern:
        raise ConfigError(
            f'tempFileNamePattern "{pattern}" must contain {{name}}, add something to it, and not contain a path'
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
        timeout=parse_duration("timeout", get("timeout"), DEFAULT_TIMEOUT),
        temp_file_name_pattern=pattern,
        inbound_path=normalize_path(get("inboundPath")) if get("inboundPath") else root_path,
        file_patterns=_list(get("filePattern")) or ("*",),
        ignore_patterns=_list(get("ignorePatterns")),
        poll_interval=max(parse_duration("pollInterval", get("pollInterval"), DEFAULT_POLL_INTERVAL), 0.1),
        processed_folder=normalize_path(get("processedFolder")) or "processed",
        error_folder=normalize_path(get("errorFolder")) or "error",
    )
    return cfg, warns


def _parse_address(addr: str) -> tuple[str, int]:
    if not addr:
        raise ConfigError(
            'address is required: the file server FQDN registered in AD, e.g. "fs01.corp.example.com"'
        )
    host, _, port_s = addr.partition(":")
    if not port_s:
        port = DEFAULT_PORT
    elif port_s.isdigit() and 1 <= int(port_s) <= 65535:
        port = int(port_s)
    else:
        raise ConfigError(f'address "{addr}" must be a host name, optionally with :port')
    if not host or "\\" in host or "/" in host:
        raise ConfigError(f'address "{addr}" must be a host name, not a URL or UNC path')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host.lower(), port
    raise ConfigError(
        f'address "{host}" is an IP address. Kerberos needs the name the server is registered under in AD '
        "(SPN cifs/<fqdn>); map it with pod hostAliases if the customer's DNS is not reachable."
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
