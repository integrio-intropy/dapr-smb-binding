from __future__ import annotations

import base64
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from localfs import LocalFS  # noqa: E402

from dapr_smb_binding.config import parse  # noqa: E402

BASE = {
    "address": "fs01.corp.example.com",
    "share": "exchange",
    "rootPath": "out",
    "username": "svc-intropy@CORP.EXAMPLE.COM",
    "password": "s3cret",
}

# A syntactically valid (empty) MIT keytab: version 0x0502, no entries.
KEYTAB_B64 = base64.b64encode(b"\x05\x02").decode()


def make_cfg(**overrides: str):
    props = {**BASE, **overrides}
    for k, v in list(props.items()):
        if v is None:
            del props[k]
    cfg, _ = parse(props)
    return cfg


@pytest.fixture
def lfs(tmp_path: pathlib.Path) -> LocalFS:
    return LocalFS(tmp_path)
