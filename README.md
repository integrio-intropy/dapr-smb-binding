# dapr-smb-binding

A [Dapr pluggable component](https://docs.dapr.io/developing-applications/develop-components/pluggable-components/pluggable-components-overview/)
providing an **SMB input and output binding** for Windows file shares, from
Linux containers, with **Kerberos only** by default.

The SMB client runs in user space inside the component container
([smbprotocol](https://github.com/jborean93/smbprotocol) and MIT krb5 through
GSSAPI). There is no CSI driver, no privileged mount, no node configuration
and nothing installed at the customer: a password or a keytab in a secret is
enough, and tickets stay in memory.

```bash
curl -X POST localhost:3500/v1.0/bindings/partner-share \
  -d '{"operation":"create","data":"<order/>","metadata":{"fileName":"order-1.xml"}}'
```

(Dapr's HTTP API JSON-encodes a string `data`, so that file contains
`"\u003corder/\u003e"`. The SDKs, over gRPC, send raw bytes. This is the same
for every binding, SFTP and FTP included.)

## Status

Alpha. Operations and metadata follow Dapr's SFTP binding, extended by the
shared Intropy file binding contract in [CONTRACT.md](CONTRACT.md) (temp-file
and completion handling, `rename`, and an input binding). The same contract
applies to the FTP and SFTP bindings, so the FileAdapter does not care which
protocol a share uses.

| Operation | Behaviour |
|---|---|
| `create` | Write the request data to `fileName` via a temp file and an atomic rename. Never overwrites by default. |
| `get` | Read `fileName` |
| `list` | List the folder in `fileName`, defaulting to `rootPath` |
| `delete` | Delete `fileName` |
| `rename` | Rename `fileName` to `newFileName`. Never overwrites by default. |
| input | Poll `inboundPath`, claim each finished file by renaming it, deliver it, then move it to `processed/` or `error/` |

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

### Connection and authentication

| Field | Required | Default | Notes |
|---|---|---|---|
| `address` | **yes** | — | File server FQDN, optionally `:port`. Must match an SPN `cifs/<fqdn>`. An IP is rejected under Kerberos. |
| `share` | **yes** | — | Share name only. |
| `rootPath` | **yes** | — | Folder inside the share; `/` for the share root. Joined, not confined. |
| `authMode` | no | `kerberos-password` | `kerberos-password`, `kerberos-keytab`, or `ntlm` (explicit opt-in, never a fallback). |
| `username` | for password and ntlm | — | `user@REALM`, or `user` / `DOMAIN\user` with `realm` set. In keytab mode, selects the principal. |
| `password` | for password and ntlm | — | Use `secretKeyRef`. Never logged. |
| `keytab` | for keytab | — | The keytab file, base64. Must hold AES keys. Use `secretKeyRef`. |
| `realm` | no | from `username` | Kerberos realm, upper case. |
| `kdcs` | no | DNS | Comma-separated KDC host names. Set them unless the pod resolves the customer's `_kerberos._tcp` SRV records. |
| `requireEncryption` | no | `true` | Refuse a session the server will not encrypt. |
| `timeout` | no | `30s` | Connecting and session setup. Needs a unit. |

Completion settings (`tempFileMode`, `onTargetExists`, `staleTempFileAge`,
`completionMarkerPattern`, `claimMode`, `requireMarkerPattern`, `stableFor`,
`afterRead`, and the folder names) are described in
[CONTRACT.md](CONTRACT.md#write-side-completion-settings-ext). **The defaults
are safe**: temp name `{name}.tmp` then rename, never overwrite, claim by
rename, move to `processed/` after the app acknowledges.

## How the guarantees hold

**Partners never see a half-written file.** `create` writes `name.tmp` with an
exclusive open, checks the size, and renames it to `name`. SMB rename is
atomic and refuses to overwrite. Agree with the partner that they ignore
`*.tmp`, or use `tempFileMode: folder` if they pick up `*`.

**We never read a half-written file.** The input binding claims a file by
renaming it into `processing/<pod>/`. While the partner still has it open (a
normal Windows writer does not allow delete-sharing), the server refuses with
`STATUS_SHARING_VIOLATION`, and the file is retried on the next poll. The
read itself is exclusive. Partners that write in place and close early need
`requireMarkerPattern` or `stableFor`.

**Several replicas** are safe: only one wins each rename. A replica that goes
away with files claimed but not yet acknowledged (crash, rollout) stops
writing its heartbeat; after `staleClaimAge` another replica returns those
files to the inbound folder.

**Ticket expiry.** When the Kerberos ticket expires the server ends the SMB
session (`STATUS_NETWORK_SESSION_EXPIRED`). smbprotocol does not
re-authenticate on its own, so the binding drops the connection, sets up a new
session (which gets a fresh ticket from the password or keytab) and retries
the operation once.

If the connection drops after the server has carried out a request but before
its reply arrives, the retry would find the work already done. The binding
recognises that instead of reporting a spurious error: a rename whose source is
gone and whose target exists, or a delete whose file is gone, counts as done,
and a temp file the first attempt created is overwritten by the retry rather
than refused as "already exists".

**Account lockout.** After an authentication failure the binding backs off
(15 s, doubling to 15 min) and refuses further attempts in between, so a
wrong password cannot lock the account out through retries.

## Known limitations

- **One SMB connection per component instance.** Concurrent requests share it
  (smbprotocol multiplexes them). When one request finds the session expired
  and reconnects, requests in flight on the same connection fail too and each
  reconnects on its own retry. The result is correct but costs extra session
  setups. This only matters at high request rates; a small connection pool,
  like the FTP binding's, would remove it.
- **One inbound file at a time per component.** The input binding waits for the
  app to acknowledge a file before delivering the next. Throughput is one file
  per app round trip.
- **No ack timeout of its own.** If the app never answers, nothing else is
  delivered on that component. Set a Dapr resiliency timeout on the
  component's inbound direction; see
  [CONTRACT.md](CONTRACT.md#ack-timeout). It turns a hang into a nack (the
  file goes to `errorFolder`), but the app may still finish the work, so the
  adapter must be idempotent.
- **Taking over a gone replica's claims waits `staleClaimAge`** (15 min by
  default) after its last heartbeat. Until then those files are not delivered.
- **Files are single messages.** Neither Dapr's binding API nor the component
  protocol streams, so a file must fit in `--max-message-size` (and the
  sidecar's `dapr.io/max-body-size`). There is no claim-check for larger files
  yet; inbound files over the limit go to `errorFolder`.
- **`overwrite` after a lost reply.** When a `replace` is retried after a
  reconnect and finds its source gone and the target present, it is taken as
  done. For a plain rename that is conclusive; for `replace` the target may
  have existed before, so it is the best evidence available.
- **Delete after a lost reply** reports success when the file is gone, even
  in the unlikely case that the first attempt never reached the server and the
  file never existed.
- **Not yet validated against Windows Server + AD** or across the real VPN; see
  [poc/README.md](poc/README.md).

## Running it

See [`examples/kubernetes`](examples/kubernetes) and
[`examples/standalone`](examples/standalone). Customer onboarding (network,
AD and share prerequisites) is in [docs/onboarding.md](docs/onboarding.md).

In Kubernetes, mount an `emptyDir` with `medium: Memory` at
`/tmp/dapr-smb-binding`: the generated `krb5.conf` and the keytab (in keytab
mode) are written there, and this keeps the key off node disks and works with
a read-only root file system.

### Health

The sidecar does not reopen an input stream that breaks, and its own health
endpoint does not check components. Run with `--metrics-port` and point the
container's liveness probe at `/healthz`: it fails when an input stream has
closed, so Kubernetes restarts the container and the sidecar reconnects.
`Ping` verifies the session and tree connect.

### Metrics

With `--metrics-port`, Prometheus metrics on `/metrics`:
`smb_binding_operations_total`, `smb_binding_operation_seconds`,
`smb_binding_files_claimed_total`, `smb_binding_files_processed_total`,
`smb_binding_files_failed_total`, `smb_binding_sharing_violations_total`,
`smb_binding_reconnects_total`, `smb_binding_claims_recovered_total`, `smb_binding_auth_failures_total`,
`smb_binding_stale_temp_files_removed_total`, `smb_binding_input_stream_up`.
Every series carries `component` and `target`.

## Command-line flags

The component is configured through Dapr metadata; these only cover the process.

| Flag | Default | Notes |
|---|---|---|
| `--socket-name` | `smb` | Socket file name without `.sock`. Determines `spec.type` (`bindings.<name>`). |
| `--socket-perm` | `0660` | Socket permission bits, read as octal. |
| `--max-message-size` | `5242880` | Maximum gRPC message in bytes. Raise with the sidecar's `dapr.io/max-body-size`. |
| `--instance-id` | `$POD_NAME`, `$HOSTNAME` | Names this replica's processing folder. |
| `--metrics-port` | `0` (off) | Serve `/metrics` and `/healthz`. |
| `--log-level` | `info` | `debug`, `info`, `warn`, `error`. |
| `--log-format` | `text` | `text` or `json`. |
| `--version` | | Print the version and exit. |

## Development

```bash
make test           # unit tests; Python 3.12 only, no SMB server or Kerberos needed
make lint
make integration    # Samba AD DC in Docker: NTLM disabled, SMB 3 encryption required
SMB_IT_EXPIRY=1 make integration   # also the ticket-expiry test (~8 min)
make e2e            # the production image under a real daprd sidecar, input and output
make integration-down
```

The unit tests run the binding against a local-directory fake with SMB's
rename and sharing semantics, and the gRPC services over a real Unix socket.
The proof-of-concept scripts the design came from are in [poc/](poc/).

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Dapr is a trademark of the Linux Foundation. This project is a community
component and is not affiliated with or endorsed by the Dapr project.
