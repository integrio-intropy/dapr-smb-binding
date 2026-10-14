"""Against a real Samba AD DC with NTLM disabled and encryption required.

Ports the PoC (poc/smb_poc.py, poc/renew_test.py) to automated tests and adds
the completion settings. Run with `make integration`.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import time
import uuid

import pytest

from dapr_smb_binding import metrics
from dapr_smb_binding.config import parse
from dapr_smb_binding.errors import Kind, SmbError
from dapr_smb_binding.inbound import Inbound
from dapr_smb_binding.output import OutputBinding
from dapr_smb_binding.smb import SmbFileSystem

pytestmark = pytest.mark.integration

E = os.environ


def _env(name: str) -> str:
    v = E.get(name)
    if not v:
        pytest.skip(f"{name} not set: run through `make integration`")
    return v


@pytest.fixture
def base_dir() -> str:
    # Every test gets its own folder on the share.
    return f"it/{uuid.uuid4().hex[:10]}"


def props(base: str, **over: str) -> dict[str, str]:
    p = {
        "address": _env("SMB_IT_ADDRESS"),
        "share": _env("SMB_IT_SHARE"),
        "rootPath": f"{base}/out",
        "inboundPath": f"{base}/in",
        "username": _env("SMB_IT_USER"),
        "password": _env("SMB_IT_PASSWORD"),
        "realm": _env("SMB_IT_REALM"),
        "kdcs": _env("SMB_IT_KDC"),
        "timeout": "20s",
    }
    p.update(over)
    return {k: v for k, v in p.items() if v is not None}


def open_fs(base: str, name: str = "it", **over: str) -> SmbFileSystem:
    cfg, _ = parse(props(base, **over))
    return SmbFileSystem(cfg, component=f"{name}-{uuid.uuid4().hex[:6]}")


@pytest.fixture
def fs(base_dir):
    f = open_fs(base_dir)
    f.makedirs(f"{base_dir}/out")
    f.makedirs(f"{base_dir}/in")
    yield f
    f.close()


class Partner:
    """The partner's writer, on its own connection and credentials."""

    def __init__(self, base: str):
        import smbclient

        self.smbclient = smbclient
        self.cache: dict = {}
        self.kw = {
            "username": _env("SMB_IT_PARTNER_USER"),
            "password": _env("SMB_IT_PARTNER_PASSWORD"),
            "auth_protocol": "kerberos",
            "encrypt": True,
            "connection_cache": self.cache,
        }
        self.root = f"\\\\{_env('SMB_IT_ADDRESS')}\\{_env('SMB_IT_SHARE')}\\" + base.replace("/", "\\")

    def unc(self, rel: str) -> str:
        return self.root + "\\" + rel.replace("/", "\\")

    def open_for_write(self, rel: str):
        # A typical Windows writer: read sharing only, so no rename/delete by others.
        return self.smbclient.open_file(self.unc(rel), mode="wb", share_access="r", **self.kw)

    def write(self, rel: str, data: bytes = b"") -> None:
        self.smbclient.makedirs(self.unc(rel.rsplit("/", 1)[0]), exist_ok=True, **self.kw)
        with self.smbclient.open_file(self.unc(rel), mode="wb", **self.kw) as f:
            f.write(data)

    def age(self, rel: str, seconds: float) -> None:
        """Set the server-side access and modification time `seconds` into the past."""
        t = time.time() - seconds
        self.smbclient.utime(self.unc(rel), times=(t, t), **self.kw)

    def close(self):
        self.smbclient.reset_connection_cache(fail_on_error=False, connection_cache=self.cache)


@pytest.fixture
def partner(base_dir):
    p = Partner(base_dir)
    yield p
    p.close()


# ---------------------------------------------------------------- authentication


def test_ntlm_is_refused(base_dir):
    cfg, _ = parse(props(base_dir, authMode="ntlm", username="svc-intropy", realm=None, kdcs=None))
    fs = SmbFileSystem(cfg, component="ntlm")
    with pytest.raises(SmbError) as e:
        fs.ping()
    assert e.value.kind in (Kind.CONFIG, Kind.AUTH)
    fs.close()


def test_kerberos_password_without_kinit_is_encrypted(fs, base_dir):
    fs.ping()
    sessions = [s for c in fs._cache.values() for s in c.session_table.values()]
    assert sessions and all(s.encrypt_data for s in sessions)
    assert fs._cache and all(c.dialect >= 0x0300 for c in fs._cache.values())


def test_kerberos_keytab(base_dir):
    keytab = pathlib.Path(_env("SMB_IT_KEYTAB")).read_bytes()
    fs = open_fs(
        base_dir, authMode="kerberos-keytab", keytab=base64.b64encode(keytab).decode(), password=None
    )
    fs.makedirs(f"{base_dir}/out")
    fs.ping()
    fs.close()


def test_wrong_password_backs_off(base_dir):
    fs = open_fs(base_dir, password="wrong")
    with pytest.raises(SmbError, match="authentication failed"):
        fs.ping()
    with pytest.raises(SmbError, match="not retrying authentication"):
        fs.ping()
    fs.close()


# ---------------------------------------------------------------- output


def test_create_get_list_rename_delete(fs, base_dir):
    cfg, _ = parse(props(base_dir))
    b = OutputBinding(cfg, fs)
    r = b.invoke("create", b"<a/>", {"fileName": "a.xml"})
    assert json.loads(r.data) == {"fileName": "a.xml"}
    assert b.invoke("get", b"", {"fileName": "a.xml"}).data == b"<a/>"
    names = [e["fileName"] for e in json.loads(b.invoke("list", b"", {}).data)]
    assert names == ["a.xml"]  # temp file is gone

    with pytest.raises(SmbError) as e:
        b.invoke("create", b"<b/>", {"fileName": "a.xml"})
    assert e.value.kind is Kind.EXISTS
    assert b.invoke("get", b"", {"fileName": "a.xml"}).data == b"<a/>"

    b.invoke("create", b"<c/>", {"fileName": "c.xml"})
    with pytest.raises(SmbError) as e:
        b.invoke("rename", b"", {"fileName": "c.xml", "newFileName": "a.xml"})
    assert e.value.kind is Kind.EXISTS

    b.invoke("rename", b"", {"fileName": "c.xml", "newFileName": "a.xml", "onTargetExists": "overwrite"})
    assert b.invoke("get", b"", {"fileName": "a.xml"}).data == b"<c/>"
    b.invoke("delete", b"", {"fileName": "a.xml"})
    assert json.loads(b.invoke("list", b"", {}).data) == []


@pytest.mark.parametrize(
    "over",
    [
        {"tempFileMode": "folder"},
        {"tempFileMode": "none"},
        {"onTargetExists": "uniqueName"},
        {"completionMarkerPattern": "{name}.done"},
    ],
)
def test_completion_variants(fs, base_dir, over):
    cfg, _ = parse(props(base_dir, **over))
    b = OutputBinding(cfg, fs)
    b.invoke("create", b"1", {"fileName": "a.xml"})
    if over.get("onTargetExists") == "uniqueName":
        r = b.invoke("create", b"2", {"fileName": "a.xml"})
        assert json.loads(r.data) == {"fileName": "a_1.xml"}
    names = sorted(e.name for e in fs.listdir(f"{base_dir}/out") if not e.is_dir)
    expected = {"completionMarkerPattern": ["a.xml", "a.xml.done"], "onTargetExists": ["a.xml", "a_1.xml"]}
    assert names == next((v for k, v in expected.items() if k in over), ["a.xml"])


def test_eight_mib_roundtrip(fs, base_dir):
    cfg, _ = parse(props(base_dir))
    b = OutputBinding(cfg, fs)
    data = os.urandom(8 << 20)
    b.invoke("create", data, {"fileName": "big.bin"})
    assert b.invoke("get", b"", {"fileName": "big.bin"}).data == data


# ---------------------------------------------------------------- inbound


def inbound(fs, base_dir, instance="it-pod", **over):
    cfg, _ = parse(props(base_dir, **over))
    return Inbound(fs, cfg.read, cfg.write, instance_id=instance, max_file_size=16 << 20)


def test_claim_waits_for_partner_to_close(fs, base_dir, partner):
    ib = inbound(fs, base_dir)
    f = partner.open_for_write("in/order.xml")
    f.write(b"<order>")
    f.flush()
    try:
        assert ib.poll() == []  # STATUS_SHARING_VIOLATION while the partner writes
        f.write(b"</order>")
    finally:
        f.close()
    [c] = ib.poll()
    m = ib.load(c)
    assert m.data == b"<order></order>"
    ib.complete(c, ok=True)
    assert [e.name for e in fs.listdir(f"{base_dir}/in/processed")] == ["order.xml"]


def test_inbound_marker_and_error_folder(fs, base_dir, partner):
    ib = inbound(fs, base_dir, requireMarkerPattern="{name}.done")
    with partner.open_for_write("in/a.xml") as f:
        f.write(b"x")
    assert ib.poll() == []
    with partner.open_for_write("in/a.xml.done") as f:
        pass
    [c] = ib.poll()
    ib.load(c)
    ib.complete(c, ok=False, error="bad")
    assert [e.name for e in fs.listdir(f"{base_dir}/in/error")] == ["a.xml"]
    assert sorted(e.name for e in fs.listdir(f"{base_dir}/in") if not e.is_dir) == []


# ---------------------------------------------------------------- stale claims


def names(fs, path):
    return sorted(e.name for e in fs.listdir(path))


def test_claim_of_a_gone_replica_is_taken_over(fs, base_dir, partner):
    """A file claimed by a replica that died is returned and delivered by another."""
    # The partner wrote the file an hour ago; pod-b claimed it and kept its heartbeat fresh.
    partner.write("in/order.xml", b"<order/>")
    partner.age("in/order.xml", 3600)
    pod_b = inbound(fs, base_dir, instance="pod-b")
    pod_b.heartbeat()
    [claimed] = pod_b.poll()
    assert claimed.path.endswith("processing/pod-b/order.xml")

    # The premise of the design, on a real server: a rename keeps the partner's time, so
    # the claimed file looks an hour old although pod-b claimed it just now.
    on_server = fs.stat(claimed.path)
    assert time.time() - on_server.mtime.timestamp() > 3000

    # pod-a must therefore judge pod-b by its heartbeat, which is fresh: hands off.
    pod_a = inbound(fs, base_dir, instance="pod-a", staleClaimAge="15m")
    assert pod_a.sweep_stale_claims() == 0
    assert names(fs, f"{base_dir}/in/processing/pod-b") == [".heartbeat", "order.xml"]

    # pod-b dies: its heartbeat stops and ages past staleClaimAge.
    partner.age("in/processing/pod-b/.heartbeat", 3600)
    [taken] = pod_a.poll()
    assert taken.path.endswith("processing/pod-a/order.xml")
    m = pod_a.load(taken)
    assert m.data == b"<order/>"
    pod_a.complete(taken, ok=True)

    assert names(fs, f"{base_dir}/in/processed") == ["order.xml"]
    # pod-b's folder is cleaned up; only pod-a's remains.
    assert names(fs, f"{base_dir}/in/processing") == ["pod-a"]


def test_old_folder_without_heartbeat_is_taken_over(fs, base_dir, partner):
    partner.write("in/processing/crashed/a.xml", b"a")
    partner.age("in/processing/crashed/a.xml", 3600)
    partner.age("in/processing/crashed", 3600)
    pod_a = inbound(fs, base_dir, instance="pod-a", staleClaimAge="15m")
    assert pod_a.sweep_stale_claims() == 1
    assert names(fs, f"{base_dir}/in/processing") == []
    assert "a.xml" in names(fs, f"{base_dir}/in")


def test_stale_claims_left_alone_when_disabled(fs, base_dir, partner):
    partner.write("in/processing/old/a.xml", b"a")
    partner.write("in/processing/old/.heartbeat")
    partner.age("in/processing/old/.heartbeat", 3600)
    assert inbound(fs, base_dir, instance="pod-a", staleClaimAge="").sweep_stale_claims() == 0
    assert names(fs, f"{base_dir}/in/processing/old") == [".heartbeat", "a.xml"]


# ---------------------------------------------------------------- session expiry


@pytest.mark.skipif(os.environ.get("SMB_IT_EXPIRY") != "1", reason="slow: set SMB_IT_EXPIRY=1")
@pytest.mark.parametrize("mode", ["kerberos-password", "kerberos-keytab"])
def test_reconnects_after_ticket_expiry(base_dir, mode, monkeypatch):
    # Ask the KDC for 3-minute tickets; the server ends the session when they expire.
    from dapr_smb_binding import kerberos

    real = kerberos.render
    monkeypatch.setattr(
        kerberos,
        "render",
        lambda specs: real(specs).replace("[libdefaults]", "[libdefaults]\n    ticket_lifetime = 3m"),
    )
    over: dict[str, str | None] = {"authMode": mode}
    if mode == "kerberos-keytab":
        over |= {
            "keytab": base64.b64encode(pathlib.Path(_env("SMB_IT_KEYTAB")).read_bytes()).decode(),
            "password": None,
        }
    fs = open_fs(base_dir, **over)
    fs.makedirs(f"{base_dir}/out")
    labels = {"component": fs.component, "target": fs.cfg.target}
    before = metrics.RECONNECTS.labels(**labels)._value.get()
    time.sleep(4 * 60)
    fs.ping()
    assert metrics.RECONNECTS.labels(**labels)._value.get() == before + 1
    fs.close()
