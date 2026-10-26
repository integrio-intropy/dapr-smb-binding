"""Against a real Samba AD DC with NTLM disabled and encryption required.

Covers the cases the design was validated with in INTROPY-263. Run with
`make integration`.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import time
import uuid

import pytest

from dapr_smb_binding.config import parse
from dapr_smb_binding.errors import Kind, SmbError
from dapr_smb_binding.inbound import Inbound
from dapr_smb_binding.output import OutputBinding
from dapr_smb_binding.smb import SmbFileSystem

pytestmark = pytest.mark.integration


def env(name: str) -> str:
    if not os.environ.get(name):
        pytest.skip(f"{name} not set: run through `make integration`")
    return os.environ[name]


@pytest.fixture
def base() -> str:
    return f"it/{uuid.uuid4().hex[:10]}"  # every test gets its own folder


def config(base: str, **over: str | None):
    props = {
        "address": env("SMB_IT_ADDRESS"),
        "share": env("SMB_IT_SHARE"),
        "rootPath": f"{base}/out",
        "inboundPath": f"{base}/in",
        "username": env("SMB_IT_USER"),
        "password": env("SMB_IT_PASSWORD"),
        "kdcs": env("SMB_IT_KDC"),
    } | over
    return parse({k: v for k, v in props.items() if v is not None})[0]


@pytest.fixture
def fs(base):
    f = SmbFileSystem(config(base), component=f"it-{uuid.uuid4().hex[:6]}")
    f.makedirs(f"{base}/out")
    f.makedirs(f"{base}/in")
    yield f
    f.close()


@pytest.fixture
def partner(base):
    """The partner's writer, on its own connection and account."""
    import smbclient

    cache: dict = {}
    kw = {
        "username": env("SMB_IT_PARTNER_USER"),
        "password": env("SMB_IT_PARTNER_PASSWORD"),
        "auth_protocol": "kerberos",
        "connection_cache": cache,
    }
    root = f"\\\\{env('SMB_IT_ADDRESS')}\\{env('SMB_IT_SHARE')}\\{base.replace('/', '\\')}"

    def open_for_write(rel: str):
        # A typical Windows writer: read sharing only, no delete sharing.
        return smbclient.open_file(f"{root}\\{rel.replace('/', '\\')}", mode="wb", share_access="r", **kw)

    yield open_for_write
    smbclient.reset_connection_cache(fail_on_error=False, connection_cache=cache)


# ---------------------------------------------------------------- authentication


def test_lab_refuses_ntlm():
    """Proves the lab really blocks NTLM, so the Kerberos tests below cannot pass through NTLM."""
    import smbclient

    with pytest.raises(Exception):  # noqa: B017 - Samba answers with an auth or NTLM-blocked error
        smbclient.register_session(env("SMB_IT_ADDRESS"), username="svc-intropy", password=env("SMB_IT_PASSWORD"),
                                   auth_protocol="ntlm", connection_cache={})  # fmt: skip


def test_kerberos_password_session_is_encrypted(fs):
    fs.ping()
    sessions = [s for c in fs._cache.values() for s in c.session_table.values()]
    assert sessions and all(s.encrypt_data for s in sessions)


def test_kerberos_keytab(base):
    keytab = base64.b64encode(pathlib.Path(env("SMB_IT_KEYTAB")).read_bytes()).decode()
    fs = SmbFileSystem(
        config(base, authMode="kerberos-keytab", keytab=keytab, password=None), component="it-keytab"
    )
    fs.ping()
    fs.close()


def test_wrong_password_backs_off(base):
    fs = SmbFileSystem(config(base, password="wrong"), component="it-wrong")
    with pytest.raises(SmbError, match="authentication failed"):
        fs.ping()
    with pytest.raises(SmbError, match="not retrying login"):
        fs.ping()
    fs.close()


# ---------------------------------------------------------------- output


def test_output_operations(fs, base):
    b = OutputBinding(config(base), fs)
    assert json.loads(b.invoke("create", b"<a/>", {"fileName": "a.xml"}).data) == {"fileName": "a.xml"}
    assert [e["fileName"] for e in json.loads(b.invoke("list", b"", {}).data)] == ["a.xml"]  # no temp left
    with pytest.raises(SmbError) as e:
        b.invoke("create", b"<b/>", {"fileName": "a.xml"})
    assert e.value.kind is Kind.EXISTS
    assert b.invoke("get", b"", {"fileName": "a.xml"}).data == b"<a/>"  # not overwritten
    b.invoke("rename", b"", {"fileName": "a.xml", "newFileName": "archive/a.xml"})
    b.invoke("delete", b"", {"fileName": "archive/a.xml"})


def test_leftover_temp_file_is_overwritten(fs, base, partner):
    with partner("out/a.xml.tmp") as f:
        f.write(b"partial, left by a crash")
    b = OutputBinding(config(base), fs)
    b.invoke("create", b"complete", {"fileName": "a.xml"})
    assert b.invoke("get", b"", {"fileName": "a.xml"}).data == b"complete"
    assert [e.name for e in fs.listdir(f"{base}/out")] == ["a.xml"]


def test_temp_file_another_writer_has_open_is_refused(fs, base, partner):
    b = OutputBinding(config(base), fs)
    with partner("out/a.xml.tmp") as f:
        f.write(b"theirs, still being written")
        f.flush()
        with pytest.raises(SmbError) as e:
            b.invoke("create", b"ours", {"fileName": "a.xml"})
        assert e.value.kind is Kind.SHARING_VIOLATION
    assert [e.name for e in fs.listdir(f"{base}/out")] == ["a.xml.tmp"]  # theirs, untouched


def test_eight_mib_roundtrip(fs, base):
    b = OutputBinding(config(base), fs)
    data = os.urandom(8 << 20)
    b.invoke("create", data, {"fileName": "big.bin"})
    assert b.invoke("get", b"", {"fileName": "big.bin"}).data == data


def test_exclusive_get_waits_for_the_partner_to_close(fs, base, partner):
    b = OutputBinding(config(base), fs)
    f = partner("out/order.xml")
    f.write(b"<order>")
    f.flush()
    try:
        # A sweep through list + get: the listing shows the file, an exclusive read is refused.
        assert [e["fileName"] for e in json.loads(b.invoke("list", b"", {}).data)] == ["order.xml"]
        with pytest.raises(SmbError) as e:
            b.invoke("get", b"", {"fileName": "order.xml", "exclusive": "true"})
        assert e.value.kind is Kind.SHARING_VIOLATION
        f.write(b"</order>")
    finally:
        f.close()
    assert b.invoke("get", b"", {"fileName": "order.xml", "exclusive": "true"}).data == b"<order></order>"


# ---------------------------------------------------------------- inbound


def test_file_is_read_only_after_partner_closes(fs, base, partner):
    ib = Inbound(config(base), fs, max_file_size=16 << 20)
    f = partner("in/order.xml")
    f.write(b"<order>")
    f.flush()
    try:
        [entry] = ib.poll()
        assert ib.load(entry) is None  # STATUS_SHARING_VIOLATION while the partner writes
        f.write(b"</order>")
    finally:
        f.close()
    [entry] = ib.poll()
    assert ib.load(entry).data == b"<order></order>"
    ib.complete("order.xml", ok=True)
    assert [e.name for e in fs.listdir(f"{base}/in/processed")] == ["order.xml"]


# ---------------------------------------------------------------- session expiry


@pytest.mark.skipif(os.environ.get("SMB_IT_EXPIRY") != "1", reason="slow: set SMB_IT_EXPIRY=1")
@pytest.mark.parametrize("mode", ["kerberos-password", "kerberos-keytab"])
def test_reconnects_after_ticket_expiry(base, mode, monkeypatch):
    from dapr_smb_binding import kerberos

    # Ask the KDC for 3-minute tickets; the server ends the session when they expire.
    real = kerberos.render
    monkeypatch.setattr(
        kerberos,
        "render",
        lambda r: real(r).replace("[libdefaults]", "[libdefaults]\n    ticket_lifetime = 3m"),
    )
    over: dict[str, str | None] = {"authMode": mode}
    if mode == "kerberos-keytab":
        over |= {
            "keytab": base64.b64encode(pathlib.Path(env("SMB_IT_KEYTAB")).read_bytes()).decode(),
            "password": None,
        }
    fs = SmbFileSystem(config(base, **over), component=f"it-expiry-{mode}")
    fs.ping()
    first = next(iter(fs._cache.values()))
    time.sleep(4 * 60)
    fs.ping()  # succeeds through a reconnect
    assert next(iter(fs._cache.values())) is not first
    fs.close()
