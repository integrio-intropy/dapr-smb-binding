# Contributing

Security issues go through
[private vulnerability reporting](https://github.com/integrio-intropy/dapr-smb-binding/security/advisories/new),
not a pull request.

## Getting set up

```bash
make test     # creates .venv, installs '.[dev]', runs the unit tests
make lint
```

The unit tests need only Python 3.12: the binding runs against a
local-directory fake with SMB's semantics (`tests/unit/localfs.py`), and the
Kerberos stack is not imported.

For the integration suite you need Docker:

```bash
make integration                  # Samba AD DC + test runner in compose
SMB_IT_EXPIRY=1 make integration  # also the ~8 minute ticket-expiry test
make integration-down
```

## Rules that keep the guarantees

- **Never fall back to NTLM.** Every smbclient call passes `auth_protocol="kerberos"`
  on every smbclient call, not just `register_session`: smbclient opens a new
  session with its default (`negotiate`) if a call arrives without one.
- **Never overwrite.** `rename` refuses an existing target.
- **Start small.** Add an option when a partner or an incident needs it, and
  add it to CONTRACT.md so the FTP and SFTP bindings use the same name.
- The contract in CONTRACT.md is shared with the FTP and SFTP bindings. A
  change to it is a change to all of them.

## Regenerating the gRPC stubs

The Dapr protos are vendored in `proto/`. After updating them, run
`make proto` and commit the result; CI checks the stubs are current.
