# dapr-smb-binding

A [Dapr pluggable component](https://docs.dapr.io/developing-applications/develop-components/pluggable-components/pluggable-components-overview/)
providing an **SMB input and output binding** for Windows file shares, from
Linux containers, with **Kerberos only**.

The SMB client runs in user space inside the component container
([smbprotocol](https://github.com/jborean93/smbprotocol) and MIT krb5 through
GSSAPI). There is no CSI driver, no privileged mount, no node configuration and
nothing installed at the customer: a password or a keytab in a secret is
enough, and tickets stay in memory.

## Status

Alpha, deliberately minimal: it does what the requirements need and nothing
more, and grows when a partner or an incident needs it. Operations and metadata
follow Dapr's SFTP binding; [CONTRACT.md](CONTRACT.md) lists the shared names.

| Operation | Behaviour |
|---|---|
| `create` | Write to a temp name, then rename to `fileName`. Never overwrites. Creates missing folders. |
| `get` | Read `fileName`. |
| `list` | List the folder in `fileName`, defaulting to `rootPath`. |
| `delete` | Delete `fileName`. |
| `rename` | Rename `fileName` to `newFileName`. Never overwrites. |
| input | Poll `inboundPath`, deliver each finished file, then move it to `processed/` (app answered OK) or `error/`. |

## Configuration

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: partner-share
spec:
  type: bindings.smb
  version: v1
  metadata:
    - name: address              # FQDN registered in AD (SPN cifs/<fqdn>). Never an IP.
      value: fs01.corp.example.com
    - name: share
      value: exchange
    - name: rootPath             # where create/get/list/delete/rename work
      value: in/from-intropy
    - name: inboundPath          # what the input binding polls
      value: out/to-intropy
    - name: filePattern
      value: "*.xml"
    - name: username
      value: svc-intropy@CORP.EXAMPLE.COM
    - name: password
      secretKeyRef: { name: partner-smb, key: password }
    - name: kdcs                 # explicit KDCs: no dependency on the customer's DNS
      value: dc1.corp.example.com,dc2.corp.example.com
```

| Field | Default | Notes |
|---|---|---|
| `address` | required | File server FQDN, optionally `:port`. Must match an SPN `cifs/<fqdn>`; an IP is rejected. |
| `share` | required | Share name only. |
| `rootPath` | required | Folder inside the share for output operations; `/` for the root. Joined, not confined. |
| `authMode` | `kerberos-password` | Or `kerberos-keytab`. There is no NTLM. |
| `username` | | `user@REALM`, or `user` with `realm`. In keytab mode it selects the principal. |
| `password` | | Use `secretKeyRef`. Never logged. |
| `keytab` | | Keytab file, base64, with AES keys. Use `secretKeyRef`. |
| `realm` | from `username` | |
| `kdcs` | DNS | Comma-separated KDCs. |
| `requireEncryption` | `true` | Refuse a session the server will not encrypt. |
| `timeout` | `30s` | Connecting. Needs a unit. |
| `tempFileNamePattern` | `{name}.tmp` | Name `create` writes before renaming, e.g. `.{name}`. Never picked up by the input binding. |
| `inboundPath` | `rootPath` | Folder the input binding polls. |
| `filePattern` | `*` | Comma-separated globs to pick up. |
| `ignorePatterns` | | Comma-separated globs never picked up, e.g. the partner's own temp names. |
| `pollInterval` | `10s` | |
| `processedFolder` / `errorFolder` | `processed` / `error` | Inside `inboundPath`. Name collisions get `_1`, `_2`, … |

## How the guarantees hold

**Partners never see a half-written file.** `create` writes `name.tmp` with an
exclusive open and renames it to `name`. SMB rename is atomic and refuses to
overwrite. Agree with the partner that they ignore `*.tmp` (or set
`tempFileNamePattern` to a name they ignore).

**We never read a half-written file.** The input binding reads each file with
an exclusive open. While the partner still has it open, the server refuses with
`STATUS_SHARING_VIOLATION` and the file is tried again on the next poll. A
partner that writes in several separate opens needs to write under a temp name
and rename, like we do.

**No file is lost.** A file stays in `inboundPath` until the app has answered,
and is only then moved. After a crash or restart it is simply delivered again:
at least once, so the adapter must be idempotent.

**Ticket expiry.** When the Kerberos ticket expires, the server ends the SMB
session (`STATUS_NETWORK_SESSION_EXPIRED`) and smbprotocol does not log in
again by itself. Every operation reconnects and retries once; the new session
gets a fresh ticket.

**Account lockout.** After a failed login the binding waits 60 s before trying
again, so retries from the poll loop and the app cannot lock the account out.

## Limits, on purpose

Each of these is a known extension, to add when it is needed:

- **One replica per input component.** Two would deliver the same file twice.
  Scaling out needs a claim step, such as renaming a file into a
  per-replica folder before reading it.
- **One inbound file at a time**, waiting for the app's answer before the next.
  No ack timeout of its own: bound it with a Dapr resiliency timeout on the
  component's inbound direction (see CONTRACT.md).
- **Files are single messages** (4 MiB by default). Raise `--max-message-size`
  with the sidecar's `dapr.io/max-body-size`; inbound files over the limit go to
  `errorFolder`.
- **No overwrite, unique names, markers, stability checks or stale temp
  cleanup.** A crash between write and rename leaves `name.tmp`, which makes the
  next `create` of that name fail with "exists" until it is removed.
- **Not yet validated against Windows Server and real AD.**

## Running it

See [examples/kubernetes](examples/kubernetes) and
[docs/onboarding.md](docs/onboarding.md). Mount a memory-backed `emptyDir` at
`/tmp/dapr-smb-binding` for the generated `krb5.conf` and keytab, and point the
container's liveness probe at `/healthz` (`--health-port`): it fails when an
input stream has closed, which the sidecar never reopens by itself.

| Flag | Default | |
|---|---|---|
| `--socket-name` | `smb` | Socket file name; `spec.type` is `bindings.<name>`. |
| `--max-message-size` | `5242880` | Raise with the sidecar's `dapr.io/max-body-size`. |
| `--health-port` | `0` (off) | Serve `/healthz`. |
| `--log-level` | `info` | `debug`, `info`, `warning`, `error`. |

## Development

```bash
make test           # unit tests; Python 3.12, no SMB server or Kerberos needed
make lint
make integration    # Samba AD DC in Docker: NTLM disabled, SMB 3 encryption required
SMB_IT_EXPIRY=1 make integration   # plus the ticket-expiry test (~8 min)
make e2e            # the image under a real daprd sidecar
make integration-down
```

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Dapr is a trademark of
the Linux Foundation; this project is not affiliated with the Dapr project.
