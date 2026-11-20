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
| `get` | Read `fileName`. With `exclusive: "true"`, refused while someone else has it open. |
| `list` | List the folder in `fileName`, defaulting to `rootPath`. Our own temp names are left out. |
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
and rename, like we do. A caller sweeping a folder with `list` and `get` gets the
same protection by sending `exclusive: "true"` with `get`.

**No file is lost.** A file stays in `inboundPath` until the app has answered,
and is only then moved. After a crash or restart it is simply delivered again:
at least once, so the adapter must be idempotent.

**Ticket expiry.** When the Kerberos ticket expires, the server ends the SMB
session (`STATUS_NETWORK_SESSION_EXPIRED`) and smbprotocol does not log in
again by itself. Every operation reconnects and retries once; the new session
gets a fresh ticket.

**Account lockout.** After a failed login the binding waits 60 s before trying
again, so retries from the poll loop and the app cannot lock the account out.

**A restarted binding is found again.** daprd connects to the binding's socket
once, while it loads its components, and never reconnects. If the binding
container restarts on its own (an OOM kill, a failed liveness probe) while
daprd keeps running, daprd holds a dead connection and every call fails with
`error reading from server: EOF`. So at startup the binding checks for that:
when daprd is up, lists a `bindings.<socket-name>` component and has not called
the binding's `Init` within 10 s, the binding asks daprd to shut down, and
Kubernetes restarts it in the same pod, connected. A daprd that does not load
the component is left alone. This needs a pod where daprd is restarted after it
exits with code 0: a Deployment, or daprd as a native sidecar, which the
kubelet restarts under any pod restart policy.

**The first login can outlast Dapr's `initTimeout`.** The binding logs in to
the share during `Init`, and the first Kerberos login in a pod can take 5 s or
more. Dapr's default `initTimeout` is 5 s, and daprd exits when it passes. Set
`initTimeout: 30s` in the Component's `spec` (see
[examples/kubernetes/component.yaml](examples/kubernetes/component.yaml)).

**Large files don't grow the process.** The image sets
`MALLOC_MMAP_THRESHOLD_=1048576`. Without it glibc raises its mmap threshold
after the first large buffer is freed, later buffers land in heap arenas that
never shrink, and repeated 16 MiB files grow the process until the memory limit
kills it. A 16 MiB file still peaks at around 200 MiB, so keep the container's
limit at 256 MiB or more.

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
  `errorFolder`. Over 4 MiB needs **Dapr 1.19 or later**: earlier sidecars read
  a component's responses (inbound files, `get`) with gRPC's 4 MiB default
  whatever `max-body-size` says ([dapr#10670](https://github.com/dapr/dapr/pull/10670)).
- **No overwrite, unique names, markers, stability checks or temp file
  cleanup.** A crash between write and rename leaves `name.tmp` behind until
  the next `create` of that name overwrites it.
- **Not yet validated against Windows Server and real AD.**

## Running it

See [examples/kubernetes](examples/kubernetes) and
[docs/onboarding.md](docs/onboarding.md). Mount a memory-backed `emptyDir` at
`/tmp/dapr-smb-binding` for the generated `krb5.conf` and keytab, and point the
container's liveness probe at `/healthz` (`--health-port`): it fails when an
input stream has closed, which the sidecar never reopens by itself.

**Jobs and CronJobs** run the binding as a Kubernetes native sidecar: an init
container with `restartPolicy: Always`, declared in the pod, with daprd
injected as a native sidecar too (`dapr.io/enable-native-sidecar: "true"`). The
kubelet starts the binding first, starts daprd once the binding is healthy,
and stops both when the app has exited. The binding needs no extra flags, and
the Job can use `restartPolicy: Never`. See
[examples/kubernetes/cronjob.yaml](examples/kubernetes/cronjob.yaml). Needs
Kubernetes 1.29 or later.

A binding that Dapr injects into a Job has to exit by itself instead
(`--exit-when-idle`), because Dapr adds it as an ordinary container that
nothing stops when the app is done. That still works, with more moving parts;
see [examples/kubernetes](examples/kubernetes/README.md#if-the-binding-is-injected-into-a-job-instead).

| Flag | Default | |
|---|---|---|
| `--socket-name` | `smb` | Socket file name; `spec.type` is `bindings.<name>`. |
| `--max-message-size` | `5242880` | Raise with the sidecar's `dapr.io/max-body-size`; over 4 MiB needs Dapr 1.19 or later. |
| `--health-port` | `0` (off) | Serve `/healthz`. |
| `--exit-when-idle` | off | Exit once the sidecar has been up and then gone this long, e.g. `30s`. Only for a binding that Dapr injects into a Job; leave it off for Deployments and for a binding that runs as a native sidecar. A pod overrides it with `SMB_BINDING_EXIT_WHEN_IDLE`. |
| `--exit-when-no-sidecar` | `1m` | With idle exit on, also exit if the sidecar never connects within this long, which is what happens when daprd dies before reaching the binding. `0` waits forever. |
| `--log-level` | `info` | `debug`, `info`, `warning`, `error`. |

| Environment | Default | |
|---|---|---|
| `SMB_BINDING_EXIT_WHEN_IDLE` | `--exit-when-idle` | The pod's override, from the annotation `smb-binding.intropy.io/exit-when-idle`: a duration, or `off` for Deployments. Unset or empty keeps the flag. Anything else stops the binding at startup. |

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
