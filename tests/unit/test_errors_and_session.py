"""NTSTATUS classification, reconnect on session expiry, and auth backoff."""

from __future__ import annotations

import sys
import types

import pytest
from conftest import make_cfg
from smbprotocol.exceptions import SMBAuthenticationError, SMBConnectionClosed, SMBOSError

from dapr_smb_binding import errors, kerberos, smb
from dapr_smb_binding.errors import Kind, SmbError, classify


class FakeResponse(Exception):
    """Shaped like the SMBResponseException smbprotocol raises for a status it has no class for."""

    def __init__(self, status: int):
        super().__init__(f"Received unexpected status from the server: 0x{status:08X}")
        self.status = status


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (errors.STATUS_SHARING_VIOLATION, Kind.SHARING_VIOLATION),
        (errors.STATUS_OBJECT_NAME_COLLISION, Kind.EXISTS),
        (errors.STATUS_OBJECT_NAME_NOT_FOUND, Kind.NOT_FOUND),
        (errors.STATUS_NETWORK_SESSION_EXPIRED, Kind.RECONNECT),
        (errors.STATUS_USER_SESSION_DELETED, Kind.RECONNECT),
        (errors.STATUS_NTLM_BLOCKED, Kind.CONFIG),
        (errors.STATUS_LOGON_FAILURE, Kind.AUTH),
        (errors.STATUS_ACCESS_DENIED, Kind.ACCESS_DENIED),
    ],
)
def test_classify_smb_os_error(status, kind):
    e = classify(SMBOSError(status, "\\\\fs01\\share\\a.xml"))
    assert e.kind is kind
    assert e.status == status


def test_classify_response_exception_without_class():
    e = classify(FakeResponse(errors.STATUS_NETWORK_SESSION_EXPIRED))
    assert e.kind is Kind.RECONNECT


def test_classify_transport_and_auth():
    assert classify(SMBConnectionClosed("closed")).kind is Kind.RECONNECT
    assert classify(ConnectionResetError()).kind is Kind.RECONNECT
    a = classify(SMBAuthenticationError("Clock skew too great"))
    assert a.kind is Kind.AUTH
    assert "5 minutes" in a.message


def test_error_string_is_one_actionable_sentence():
    e = SmbError(Kind.SHARING_VIOLATION, "sharing violation", status=errors.STATUS_SHARING_VIOLATION)
    e.with_context("get", "in/a.xml", "smb://fs01/exchange")
    s = str(e)
    assert s.startswith('smb: get "in/a.xml" on smb://fs01/exchange: ')
    assert "0xC0000043" in s
    assert "still being written" in s


# ---------------------------------------------------------------- SmbFileSystem with a fake smbclient


class FakeSmbClient(types.ModuleType):
    def __init__(self):
        super().__init__("smbclient")
        self.sessions = 0
        self.resets = 0
        self.register_errors: list[Exception] = []
        self.stat_errors: list[Exception] = []
        self.kwargs: list[dict] = []
        # A tiny server: the UNC paths that exist. None means "everything exists".
        self.files: set[str] | None = None
        # Operations that complete on the server, then lose the reply once.
        self.drop_after_once: set[str] = set()
        self.open_modes: list[str] = []

    def _maybe_drop(self, op):
        if op in self.drop_after_once:
            self.drop_after_once.discard(op)
            raise SMBConnectionClosed("connection lost before the reply")

    def _missing(self, path):
        return SMBOSError(errors.STATUS_OBJECT_NAME_NOT_FOUND, path)

    def rename(self, src, dst, **kw):
        if src not in self.files:
            raise self._missing(src)
        if dst in self.files:
            raise SMBOSError(errors.STATUS_OBJECT_NAME_COLLISION, dst)
        self.files.remove(src)
        self.files.add(dst)
        self._maybe_drop("rename")

    def remove(self, path, **kw):
        if path not in self.files:
            raise self._missing(path)
        self.files.discard(path)
        self._maybe_drop("remove")

    def open_file(self, path, mode="r", share_access=None, **kw):
        self.open_modes.append(mode)
        if mode == "xb" and path in self.files:
            raise SMBOSError(errors.STATUS_OBJECT_NAME_COLLISION, path)
        self.files.add(path)
        fake = self

        class Handle:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, data):
                fake._maybe_drop("write")

        return Handle()

    def register_session(self, server, **kw):
        self.kwargs.append(kw)
        if self.register_errors:
            raise self.register_errors.pop(0)
        self.sessions += 1
        kw["connection_cache"]["k"] = object()

    def reset_connection_cache(self, fail_on_error=True, connection_cache=None):
        self.resets += 1
        raise RuntimeError("closing a dead session can raise")  # must be ignored

    def stat(self, path, **kw):
        if self.stat_errors:
            raise self.stat_errors.pop(0)
        if self.files is not None and path.count("\\") > 3 and path not in self.files:
            raise self._missing(path)
        return types.SimpleNamespace(st_mode=0o100644, st_size=1, st_mtime=0)


@pytest.fixture
def fake_smbclient(monkeypatch, tmp_path):
    fake = FakeSmbClient()
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    monkeypatch.setattr(kerberos, "check_gssapi", lambda: None)
    monkeypatch.setattr(kerberos, "KRB5_CONFIG", kerberos.Krb5Config(tmp_path / "krb5.conf"))
    monkeypatch.setenv(kerberos.RUNTIME_DIR_ENV, str(tmp_path))
    return fake


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_kerberos_only_no_negotiate(fake_smbclient):
    fs = smb.SmbFileSystem(make_cfg(username="CORP\\svc", realm="CORP.EXAMPLE.COM"))
    fs.ping()
    kw = fake_smbclient.kwargs[0]
    assert kw["auth_protocol"] == "kerberos"
    assert kw["username"] == "svc@CORP.EXAMPLE.COM"
    assert kw["encrypt"] is True


def test_reconnects_once_on_session_expiry(fake_smbclient):
    fs = smb.SmbFileSystem(make_cfg())
    fs.ping()
    fake_smbclient.stat_errors = [FakeResponse(errors.STATUS_NETWORK_SESSION_EXPIRED)]
    fs.ping()  # succeeds after one reconnect
    assert fake_smbclient.sessions == 2
    assert fake_smbclient.resets >= 1


def test_gives_up_after_second_failure(fake_smbclient):
    fs = smb.SmbFileSystem(make_cfg())
    fake_smbclient.stat_errors = [SMBConnectionClosed("x"), SMBConnectionClosed("y")]
    with pytest.raises(SmbError) as e:
        fs.ping()
    assert e.value.kind is Kind.RECONNECT
    assert fake_smbclient.sessions == 2


def test_does_not_retry_non_session_errors(fake_smbclient):
    fs = smb.SmbFileSystem(make_cfg())
    fake_smbclient.stat_errors = [SMBOSError(errors.STATUS_ACCESS_DENIED, "x")]
    with pytest.raises(SmbError) as e:
        fs.ping()
    assert e.value.kind is Kind.ACCESS_DENIED
    assert fake_smbclient.sessions == 1


# ---------------------------------------------------------------- retry after a lost reply

UNC = "\\\\fs01.corp.example.com\\exchange\\"


def test_rename_whose_reply_was_lost_counts_as_done(fake_smbclient):
    fake_smbclient.files = {UNC + "a.tmp"}
    fake_smbclient.drop_after_once = {"rename"}
    smb.SmbFileSystem(make_cfg()).rename("a.tmp", "a.xml")
    assert fake_smbclient.files == {UNC + "a.xml"}
    assert fake_smbclient.sessions == 2  # it did reconnect


def test_rename_of_missing_file_is_not_found(fake_smbclient):
    fake_smbclient.files = set()
    fake_smbclient.stat_errors = []
    fs = smb.SmbFileSystem(make_cfg())
    fs.ping()
    fake_smbclient.drop_after_once = set()
    with pytest.raises(SmbError) as e:
        fs.rename("gone.tmp", "a.xml")
    assert e.value.kind is Kind.NOT_FOUND


def test_rename_retry_not_settled_if_target_missing(fake_smbclient):
    # Source vanished but the target is not there either: someone else took it.
    fake_smbclient.files = {UNC + "a.tmp"}
    orig = fake_smbclient.rename

    def rename(src, dst, **kw):
        if fake_smbclient.sessions == 1:
            fake_smbclient.files.clear()
            raise SMBConnectionClosed("lost")
        return orig(src, dst, **kw)

    fake_smbclient.rename = rename
    with pytest.raises(SmbError) as e:
        smb.SmbFileSystem(make_cfg()).rename("a.tmp", "a.xml")
    assert e.value.kind is Kind.NOT_FOUND


def test_delete_whose_reply_was_lost_counts_as_done(fake_smbclient):
    fake_smbclient.files = {UNC + "a.xml"}
    fake_smbclient.drop_after_once = {"remove"}
    smb.SmbFileSystem(make_cfg()).remove("a.xml")
    assert fake_smbclient.files == set()


def test_exclusive_write_retry_overwrites_its_own_file(fake_smbclient):
    fake_smbclient.files = set()
    fake_smbclient.drop_after_once = {"write"}
    smb.SmbFileSystem(make_cfg()).write("a.tmp", b"data", exclusive_create=True)
    # First attempt created the file; the retry overwrites it instead of failing on EXISTS.
    assert fake_smbclient.open_modes == ["xb", "wb"]


def test_exclusive_write_still_refuses_someone_elses_file(fake_smbclient):
    fake_smbclient.files = {UNC + "a.tmp"}
    with pytest.raises(SmbError) as e:
        smb.SmbFileSystem(make_cfg()).write("a.tmp", b"data", exclusive_create=True)
    assert e.value.kind is Kind.EXISTS
    assert fake_smbclient.open_modes == ["xb"]


def test_auth_failure_backs_off(fake_smbclient):
    clock = Clock()
    fs = smb.SmbFileSystem(make_cfg(), clock=clock)
    fake_smbclient.register_errors = [SMBAuthenticationError("Preauthentication failed")] * 3

    with pytest.raises(SmbError, match="wrong password"):
        fs.ping()
    attempts = len(fake_smbclient.kwargs)
    # Within the backoff window nothing reaches the KDC.
    with pytest.raises(SmbError, match="not retrying authentication"):
        fs.ping()
    assert len(fake_smbclient.kwargs) == attempts

    clock.t += smb.AUTH_BACKOFF_INITIAL + 1
    with pytest.raises(SmbError, match="wrong password"):
        fs.ping()
    # Second failure doubles the wait.
    clock.t += smb.AUTH_BACKOFF_INITIAL + 1
    with pytest.raises(SmbError, match="not retrying"):
        fs.ping()


def test_keytab_written_private_and_removed(fake_smbclient, tmp_path):
    import spnego
    from conftest import KEYTAB_B64

    fs = smb.SmbFileSystem(
        make_cfg(authMode="kerberos-keytab", keytab=KEYTAB_B64, password=None), component="astro"
    )
    fs.ping()
    cred = fake_smbclient.kwargs[0]["username"]
    assert isinstance(cred, spnego.KerberosKeytab)
    kt = tmp_path / "astro.keytab"
    assert kt.read_bytes() == b"\x05\x02"
    assert oct(kt.stat().st_mode & 0o777) == "0o600"
    fs.close()
    assert not kt.exists()


# ---------------------------------------------------------------- krb5.conf


def test_krb5_conf_no_dns_tcp_only():
    text = kerberos.render(
        [
            kerberos.RealmSpec("CORP.EXAMPLE.COM", ("dc1.corp", "dc2.corp"), ("fs01.corp",)),
            kerberos.RealmSpec("OTHER.TEST", ("dc.other",), ("fs.other",)),
        ]
    )
    assert "default_realm = CORP.EXAMPLE.COM" in text
    assert "dns_lookup_kdc = false" in text
    assert "udp_preference_limit = 1" in text
    assert "rdns = false" in text
    assert "kdc = dc2.corp" in text
    assert "fs.other = OTHER.TEST" in text


def test_krb5_conf_falls_back_to_dns_without_kdcs():
    assert "dns_lookup_kdc = true" in kerberos.render([kerberos.RealmSpec("CORP", (), ("fs",))])


def test_krb5_config_merges_and_unregisters(tmp_path, monkeypatch):
    monkeypatch.delenv("KRB5_CONFIG", raising=False)
    k = kerberos.Krb5Config(tmp_path / "krb5.conf")
    k.register("a", kerberos.RealmSpec("A.TEST", ("dc.a",), ("fs.a",)))
    k.register("b", kerberos.RealmSpec("B.TEST", ("dc.b",), ("fs.b",)))
    text = (tmp_path / "krb5.conf").read_text()
    assert "A.TEST = {" in text and "B.TEST = {" in text
    import os

    assert os.environ["KRB5_CONFIG"] == str(tmp_path / "krb5.conf")
    k.unregister("b")
    assert "B.TEST" not in (tmp_path / "krb5.conf").read_text()
