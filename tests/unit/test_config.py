from __future__ import annotations

import pytest
from conftest import BASE, KEYTAB_B64, make_cfg

from dapr_smb_binding.config import (
    AfterRead,
    AuthMode,
    ClaimMode,
    ConfigError,
    OnTargetExists,
    TempFileMode,
    parse,
    parse_duration,
)


def test_defaults_are_the_safe_ones():
    cfg = make_cfg()
    assert cfg.server == "fs01.corp.example.com"
    assert cfg.port == 445
    assert cfg.auth_mode is AuthMode.KERBEROS_PASSWORD
    assert cfg.realm == "CORP.EXAMPLE.COM"
    assert cfg.require_encryption is True
    w, r = cfg.write, cfg.read
    assert w.temp_file_mode is TempFileMode.NAME
    assert w.temp_file_name_pattern == "{name}.tmp"
    assert w.on_target_exists is OnTargetExists.FAIL
    assert w.stale_temp_file_age is None
    assert r.claim_mode is ClaimMode.RENAME
    assert r.after_read is AfterRead.MOVE
    assert r.inbound_path == "out"  # defaults to rootPath
    assert r.file_patterns == ("*",)
    assert r.error_folder == "error"


def test_keys_are_case_insensitive():
    cfg, _ = parse({k.upper(): v for k, v in BASE.items()} | {"TEMPFILEMODE": "folder"})
    assert cfg.write.temp_file_mode is TempFileMode.FOLDER


def test_str_never_leaks_secrets():
    cfg = make_cfg()
    assert "s3cret" not in str(cfg)
    assert "s3cret" not in repr(cfg)
    assert "<redacted>" in str(cfg)


@pytest.mark.parametrize(
    ("override", "msg"),
    [
        ({"address": ""}, "address is required"),
        ({"address": "10.0.0.5"}, "is an IP address"),
        ({"address": "\\\\fs01\\share"}, "not a URL or UNC"),
        ({"address": "fs01:99999"}, "out of range"),
        ({"share": ""}, "share is required"),
        ({"share": "exchange/sub"}, "share name only"),
        ({"rootPath": ""}, "rootPath is required"),
        ({"password": ""}, "password is required"),
        ({"authMode": "negotiate"}, "authMode"),
        ({"authMode": "kerberos-keytab"}, "keytab is required"),
        ({"keytab": "not base64!", "authMode": "kerberos-keytab"}, "base64"),
        ({"timeout": "30"}, "needs a unit"),
        ({"tempFileNamePattern": ".tmp"}, "{name}"),
        ({"tempFileNamePattern": "{name}"}, "must add something"),
        ({"tempFileMode": "rename"}, "tempFileMode"),
        ({"onTargetExists": "clobber"}, "onTargetExists"),
        ({"pollInterval": "1ms"}, "at least 100ms"),
        ({"kdcs": "dc1", "username": "svc", "realm": ""}, "realm is required"),
    ],
)
def test_rejects(override, msg):
    with pytest.raises(ConfigError, match=msg.replace("{", r"\{").replace("}", r"\}")):
        make_cfg(**override)


def test_ip_address_allowed_for_ntlm_with_warning():
    cfg, warns = parse({**BASE, "address": "10.0.0.5", "authMode": "ntlm"})
    assert cfg.auth_mode is AuthMode.NTLM
    assert any("NTLM is deprecated" in w for w in warns)


def test_short_stale_temp_age_warns():
    _, warns = parse({**BASE, "staleTempFileAge": "2m"})
    assert any("staleTempFileAge" in w for w in warns)
    _, warns = parse({**BASE, "staleTempFileAge": "1h"})
    assert not warns


def test_keytab_mode():
    cfg, warns = parse({**BASE, "authMode": "kerberos-keytab", "keytab": KEYTAB_B64, "password": ""})
    assert cfg.keytab == b"\x05\x02"
    assert not warns


def test_address_with_port_and_kdcs():
    cfg = make_cfg(address="FS01.corp.example.com:1445", kdcs="dc1.corp.example.com, dc2.corp.example.com")
    assert (cfg.server, cfg.port) == ("fs01.corp.example.com", 1445)
    assert cfg.kdcs == ("dc1.corp.example.com", "dc2.corp.example.com")
    assert cfg.target == "smb://fs01.corp.example.com:1445/exchange"


def test_paths_normalised():
    cfg = make_cfg(rootPath="\\out\\to-partner\\", inboundPath="/in/./from-partner/")
    assert cfg.root_path == "out/to-partner"
    assert cfg.read.inbound_path == "in/from-partner"


def test_explicit_empty_error_folder_means_retry():
    assert make_cfg(errorFolder="").read.error_folder == ""
    assert make_cfg(ERRORFOLDER="").read.error_folder == ""  # case-insensitive like every key
    assert make_cfg().read.error_folder == "error"


@pytest.mark.parametrize(
    ("s", "want"),
    [("10s", 10), ("1h30m", 5400), ("500ms", 0.5), ("2m", 120), ("", None), ("1.5s", 1.5)],
)
def test_duration(s, want):
    assert parse_duration("x", s) == want


@pytest.mark.parametrize("s", ["10", "ten", "10x", "s10"])
def test_bad_duration(s):
    with pytest.raises(ConfigError):
        parse_duration("x", s)


def test_stale_claim_age():
    assert make_cfg().read.stale_claim_age == 900
    assert make_cfg(staleClaimAge="1h").read.stale_claim_age == 3600
    assert make_cfg(staleClaimAge="").read.stale_claim_age is None
    _, warns = parse({**BASE, "staleClaimAge": "1m"})
    assert any("staleClaimAge" in w for w in warns)
