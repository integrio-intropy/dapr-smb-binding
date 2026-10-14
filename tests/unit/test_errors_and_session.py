"""NTSTATUS classification, Kerberos-only session setup, reconnect and auth backoff."""

from __future__ import annotations

import sys
import types

import pytest
from conftest import KEYTAB_B64, make_cfg
from smbprotocol.exceptions import SMBAuthenticationError, SMBConnectionClosed, SMBOSError

from dapr_smb_binding import errors, kerberos, smb
from dapr_smb_binding.errors import Kind, SmbError, classify


class UnknownStatus(Exception):
    """Shaped like smbprotocol's error for a status it has no class for, e.g. session expiry."""

    def __init__(self, status: int):
        super().__init__(f"Received unexpected status 0x{status:08X}")
        self.status = status


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (SMBOSError(errors.STATUS_SHARING_VIOLATION, "p"), Kind.SHARING_VIOLATION),
        (SMBOSError(errors.STATUS_OBJECT_NAME_COLLISION, "p"), Kind.EXISTS),
        (SMBOSError(errors.STATUS_OBJECT_NAME_NOT_FOUND, "p"), Kind.NOT_FOUND),
        (SMBOSError(errors.STATUS_ACCESS_DENIED, "p"), Kind.ACCESS_DENIED),
        (UnknownStatus(errors.STATUS_NETWORK_SESSION_EXPIRED), Kind.RECONNECT),
        (SMBConnectionClosed("closed"), Kind.RECONNECT),
        (ConnectionResetError(), Kind.RECONNECT),
        (SMBAuthenticationError("Preauthentication failed"), Kind.AUTH),
    ],
)
def test_classify(exc, kind):
    assert classify(exc).kind is kind


def test_error_string():
    e = SmbError(Kind.EXISTS, "exists", status=errors.STATUS_OBJECT_NAME_COLLISION)
    e.with_context("rename", "out/a.xml.tmp", "smb://fs01/exchange")
    assert str(e) == 'smb: rename "out/a.xml.tmp" on smb://fs01/exchange: exists [NTSTATUS 0xC0000035]'


# ---------------------------------------------------------------- SmbFileSystem with a fake smbclient


class FakeSmbClient(types.ModuleType):
    def __init__(self):
        super().__init__("smbclient")
        self.sessions = 0
        self.register_kwargs: list[dict] = []
        self.register_errors: list[Exception] = []
        self.stat_errors: list[Exception] = []

    def register_session(self, server, **kw):
        self.register_kwargs.append(kw)
        if self.register_errors:
            raise self.register_errors.pop(0)
        self.sessions += 1
        kw["connection_cache"]["conn"] = object()

    def reset_connection_cache(self, fail_on_error=True, connection_cache=None):
        raise RuntimeError("closing a dead session can raise")  # must be ignored

    def stat(self, path, **kw):
        assert kw["auth_protocol"] == "kerberos"  # on every call, never smbclient's default
        if self.stat_errors:
            raise self.stat_errors.pop(0)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeSmbClient()
    monkeypatch.setitem(sys.modules, "smbclient", f)
    monkeypatch.setattr(kerberos, "check_gssapi", lambda: None)
    monkeypatch.setenv(kerberos.RUNTIME_DIR_ENV, str(tmp_path))
    return f


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_kerberos_only(fake):
    smb.SmbFileSystem(make_cfg(username="CORP\\svc", realm="CORP.EXAMPLE.COM")).ping()
    kw = fake.register_kwargs[0]
    assert (kw["auth_protocol"], kw["username"], kw["encrypt"]) == ("kerberos", "svc@CORP.EXAMPLE.COM", True)


def test_keytab(fake, tmp_path):
    import spnego

    fs = smb.SmbFileSystem(
        make_cfg(authMode="kerberos-keytab", keytab=KEYTAB_B64, password=None), component="astro"
    )
    fs.ping()
    assert isinstance(fake.register_kwargs[0]["username"], spnego.KerberosKeytab)
    keytab = tmp_path / "astro.keytab"
    assert keytab.read_bytes() == b"\x05\x02"
    assert keytab.stat().st_mode & 0o777 == 0o600
    fs.close()
    assert not keytab.exists()


def test_reconnects_once_on_session_expiry(fake):
    fs = smb.SmbFileSystem(make_cfg())
    fs.ping()
    fake.stat_errors = [UnknownStatus(errors.STATUS_NETWORK_SESSION_EXPIRED)]
    fs.ping()
    assert fake.sessions == 2


def test_gives_up_after_one_retry(fake):
    fake.stat_errors = [SMBConnectionClosed("a"), SMBConnectionClosed("b")]
    with pytest.raises(SmbError) as e:
        smb.SmbFileSystem(make_cfg()).ping()
    assert e.value.kind is Kind.RECONNECT


def test_other_errors_are_not_retried(fake):
    fake.stat_errors = [SMBOSError(errors.STATUS_ACCESS_DENIED, "p")]
    with pytest.raises(SmbError):
        smb.SmbFileSystem(make_cfg()).ping()
    assert fake.sessions == 1


def test_auth_failure_backs_off(fake):
    clock = Clock()
    fs = smb.SmbFileSystem(make_cfg(), clock=clock)
    fake.register_errors = [SMBAuthenticationError("Preauthentication failed")] * 2
    with pytest.raises(SmbError, match="authentication failed"):
        fs.ping()
    with pytest.raises(SmbError, match="not retrying login"):
        fs.ping()
    assert len(fake.register_kwargs) == 1  # the KDC was not asked again
    clock.t += smb.AUTH_BACKOFF + 1
    with pytest.raises(SmbError, match="authentication failed"):
        fs.ping()
    assert len(fake.register_kwargs) == 2


def test_krb5_conf():
    text = kerberos.render(
        [("CORP.EXAMPLE.COM", ("dc1", "dc2"), "fs01"), ("OTHER.TEST", ("dc.other",), "fs.other")]
    )
    for want in ("default_realm = CORP.EXAMPLE.COM", "dns_lookup_kdc = false", "udp_preference_limit = 1",
                 "kdc = dc2", "fs.other = OTHER.TEST"):  # fmt: skip
        assert want in text
    assert "dns_lookup_kdc = true" in kerberos.render([("CORP", (), "fs")])
