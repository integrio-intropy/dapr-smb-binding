from __future__ import annotations

import pytest
from conftest import BASE, KEYTAB_B64, make_cfg

from dapr_smb_binding.config import AuthMode, ConfigError, parse, parse_duration


def test_defaults():
    cfg = make_cfg()
    assert (cfg.server, cfg.port, cfg.share, cfg.root_path) == (
        "fs01.corp.example.com",
        445,
        "exchange",
        "out",
    )
    assert cfg.auth_mode is AuthMode.KERBEROS_PASSWORD
    assert cfg.realm == "CORP.EXAMPLE.COM"
    assert cfg.require_encryption is True
    assert cfg.temp_file_name_pattern == "{name}.tmp"
    assert cfg.inbound_path == "out"  # defaults to rootPath
    assert cfg.file_patterns == ("*",)
    assert (cfg.processed_folder, cfg.error_folder) == ("processed", "error")


def test_keys_are_case_insensitive():
    cfg, _ = parse({k.upper(): v for k, v in BASE.items()} | {"POLLINTERVAL": "5s"})
    assert cfg.poll_interval == 5


def test_secrets_never_printed():
    assert "s3cret" not in str(make_cfg())
    assert "s3cret" not in repr(make_cfg())


def test_keytab_mode():
    cfg = make_cfg(authMode="kerberos-keytab", keytab=KEYTAB_B64, password=None)
    assert cfg.auth_mode is AuthMode.KERBEROS_KEYTAB
    assert cfg.keytab == b"\x05\x02"


def test_paths_and_lists():
    cfg = make_cfg(
        rootPath="\\out\\to-partner\\", inboundPath="/in/./x/", filePattern="*.xml, *.csv", kdcs="dc1,dc2"
    )
    assert cfg.root_path == "out/to-partner"
    assert cfg.inbound_path == "in/x"
    assert cfg.file_patterns == ("*.xml", "*.csv")
    assert cfg.kdcs == ("dc1", "dc2")


@pytest.mark.parametrize(
    ("override", "msg"),
    [
        ({"address": ""}, "address is required"),
        ({"address": "10.0.0.5"}, "IP address"),
        ({"address": "\\\\fs01\\share"}, "UNC"),
        ({"address": "fs01:99999"}, "optionally with :port"),
        ({"share": "exchange/sub"}, "share name only"),
        ({"rootPath": ""}, "rootPath is required"),
        ({"password": ""}, "username and password are required"),
        ({"authMode": "ntlm"}, "authMode"),
        ({"authMode": "kerberos-keytab"}, "keytab is required"),
        ({"keytab": "not base64!", "authMode": "kerberos-keytab"}, "base64"),
        ({"timeout": "30"}, "needs a unit"),
        ({"tempFileNamePattern": "{name}"}, "tempFileNamePattern"),
        ({"tempFileNamePattern": "tmp/{name}"}, "tempFileNamePattern"),
        ({"kdcs": "dc1", "username": "svc"}, "realm is required"),
    ],
)
def test_rejects(override, msg):
    with pytest.raises(ConfigError, match=msg):
        make_cfg(**override)


@pytest.mark.parametrize(("s", "want"), [("10s", 10), ("1m30s", 90), ("500ms", 0.5), ("", 7)])
def test_duration(s, want):
    assert parse_duration("x", s, 7) == want
