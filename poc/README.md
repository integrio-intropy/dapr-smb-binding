# Proof of concept (2026-10-13)

The scripts the design in INTROPY-263 was validated with, kept for reference
and for re-running against a real Windows Server + AD:

- `smb_poc.py` — NTLM refused, Kerberos with password and keytab, temp write
  and rename, rename onto an existing file, claim while a partner writes,
  64 MiB encrypted transfer.
- `renew_test.py` — session expiry with short tickets and reconnect.
- `LAB-SETUP.txt` — the Samba AD DC lab.

The same cases run automatically in `tests/integration` (`make integration`,
`SMB_IT_EXPIRY=1` for expiry) against the lab in `tests/integration/samba-dc`.

| Test | Result in the PoC |
|---|---|
| NTLM login | Refused (`STATUS_NTLM_BLOCKED`) |
| Kerberos, password, no prior kinit | SMB 3.1.1, encrypted (AES-128-GCM) |
| Kerberos, keytab only | OK |
| Temp write, rename to final | OK |
| Rename onto existing | Refused, `EEXIST` |
| Claim while partner writing | Rename and exclusive open refused (`0xC0000043`); after close, claim OK and content complete |
| 64 MiB encrypted | ~200–300 MiB/s (loopback) |
| Ticket expiry (3 min tickets, keytab) | `0xC000035C`; new session got a fresh ticket |

Still to validate: a real Windows Server + AD, throughput over the real
VPN/tunnel, and DFS paths if a customer uses them.
