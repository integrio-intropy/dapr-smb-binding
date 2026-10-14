r"""PoC: Kerberos-only, encrypted SMB from a Linux process using smbprotocol.

Server: Samba AD DC (LAB.TEST) with `ntlm auth = disabled`, `server smb encrypt = required`,
`server min protocol = SMB3`. Share: \\dc1.lab.test\exchange
"""
import errno
import os
import sys
import time
import uuid

import smbclient
from smbprotocol.exceptions import SMBOSError

SERVER = "dc1.lab.test"
SHARE = rf"\\{SERVER}\exchange"
MODE = sys.argv[1] if len(sys.argv) > 1 else "password"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append(("PASS", name, detail or ""))
    except Exception as e:  # noqa: BLE001
        results.append(("FAIL", name, f"{type(e).__name__}: {e}"))


def fresh_cache():
    return {}


# ---------------------------------------------------------------- auth
if MODE == "ntlm-negative":
    def t():
        try:
            smbclient.register_session(SERVER, username="svc-intropy@LAB.TEST",
                                       password="Svc!Passw0rd123", auth_protocol="ntlm",
                                       connection_cache=fresh_cache())
        except Exception as e:  # expected
            return f"rejected as expected -> {type(e).__name__}: {str(e)[:120]}"
        raise AssertionError("NTLM login unexpectedly succeeded")
    check("NTLM is refused by server (proves NTLM disabled)", t)

else:
    cache = fresh_cache()
    if MODE == "password":
        kw = dict(username="svc-intropy@LAB.TEST", password="Svc!Passw0rd123")
    elif MODE == "keytab":
        kw = {}  # no creds: rely on KRB5_CLIENT_KTNAME
    else:
        raise SystemExit("unknown mode")

    state = {}

    def t_auth():
        sess = smbclient.register_session(SERVER, auth_protocol="kerberos", encrypt=True,
                                          connection_cache=cache, **kw)
        state["sess"] = sess
        conn = sess.connection
        return (f"user={sess.username!r} dialect=0x{conn.dialect:04x} "
                f"encrypt_data={sess.encrypt_data} cipher={getattr(conn, 'cipher_id', None)}")
    check(f"Kerberos auth ({MODE}) + required encryption", t_auth)

    if "sess" in state:
        run = uuid.uuid4().hex[:8]
        tmp = rf"{SHARE}\in\.{run}.xml.tmp"
        final = rf"{SHARE}\in\{run}.xml"

        def t_write_rename():
            with smbclient.open_file(tmp, mode="wb", share_access=None, connection_cache=cache) as f:
                f.write(b"<doc>hello</doc>")
            smbclient.rename(tmp, final, connection_cache=cache)
            names = smbclient.listdir(rf"{SHARE}\in", connection_cache=cache)
            assert f"{run}.xml" in names and f".{run}.xml.tmp" not in names
            return "temp file written exclusively, renamed to final name"
        check("Write temp + atomic rename", t_write_rename)

        def t_no_overwrite():
            other = rf"{SHARE}\in\.{run}-2.tmp"
            with smbclient.open_file(other, mode="wb", connection_cache=cache) as f:
                f.write(b"x")
            try:
                smbclient.rename(other, final, connection_cache=cache)
            except SMBOSError as e:
                assert e.errno == errno.EEXIST, e
                smbclient.remove(other, connection_cache=cache)
                return f"rename onto existing refused: errno=EEXIST ntstatus=0x{e.ntstatus:08x}"
            raise AssertionError("rename overwrote existing file")
        check("Rename refuses to overwrite existing file", t_no_overwrite)

        def t_claim_while_writing():
            # Simulate the partner's writer on a SEPARATE connection: open for write,
            # allow readers ('r') but NOT delete sharing (typical Windows writer).
            writer_cache = fresh_cache()
            smbclient.register_session(SERVER, auth_protocol="kerberos", encrypt=True,
                                       connection_cache=writer_cache, **kw)
            busy = rf"{SHARE}\out\{run}-busy.xml"
            claimed = rf"{SHARE}\processing\{run}-busy.xml"
            w = smbclient.open_file(busy, mode="wb", share_access="r", connection_cache=writer_cache)
            w.write(b"<partial")
            w.flush()
            try:
                try:
                    smbclient.rename(busy, claimed, connection_cache=cache)
                    raise AssertionError("claimed a file that is still being written")
                except SMBOSError as e:
                    first = f"claim refused while writer open: ntstatus=0x{e.ntstatus:08x}"
                # exclusive open by reader is also refused
                try:
                    with smbclient.open_file(busy, mode="rb", share_access=None, connection_cache=cache):
                        raise AssertionError("exclusive open succeeded while writer open")
                except SMBOSError as e:
                    second = f"exclusive open refused: ntstatus=0x{e.ntstatus:08x}"
            finally:
                w.write(b"/>")
                w.close()
            smbclient.rename(busy, claimed, connection_cache=cache)
            with smbclient.open_file(claimed, mode="rb", connection_cache=cache) as f:
                data = f.read()
            assert data == b"<partial/>", data
            return f"{first}; {second}; after writer closed: claim OK, content complete"
        check("Claim-by-rename blocked while partner still writing", t_claim_while_writing)

        def t_large():
            size = 64 * 1024 * 1024
            blob = os.urandom(1024 * 1024)
            p = rf"{SHARE}\in\{run}-large.bin"
            t0 = time.time()
            with smbclient.open_file(p, mode="wb", connection_cache=cache) as f:
                for _ in range(size // len(blob)):
                    f.write(blob)
            t1 = time.time()
            n = 0
            with smbclient.open_file(p, mode="rb", connection_cache=cache) as f:
                while chunk := f.read(4 * 1024 * 1024):
                    n += len(chunk)
            t2 = time.time()
            smbclient.remove(p, connection_cache=cache)
            assert n == size
            return (f"64 MiB streamed: write {size/1048576/(t1-t0):.0f} MiB/s, "
                    f"read {size/1048576/(t2-t1):.0f} MiB/s (loopback, encrypted)")
        check("Large file streaming (encrypted)", t_large)

        smbclient.reset_connection_cache(connection_cache=cache)

for status, name, detail in results:
    print(f"[{status}] {name}\n        {detail}")
sys.exit(0 if all(r[0] == "PASS" for r in results) else 1)
