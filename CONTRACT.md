# Intropy file binding contract, v1

One contract for every file-transfer binding the Intropy FileAdapter talks
to, so the adapter is protocol-agnostic: switching a share from SMB to SFTP or
FTP is a change of `spec.type` and the connection fields, nothing else.

**The base is Dapr's built-in [SFTP binding](https://docs.dapr.io/reference/components-reference/supported-bindings/sftp/).**
Everything it defines is kept as-is. Everything else is an extension, marked
**ext** below, and every binding uses the same names and semantics for it.

| Binding | `spec.type` | Status against this contract |
|---|---|---|
| This repo | `bindings.smb` | Implements all of v1. |
| [dapr-ftp-binding](https://github.com/integrio-intropy/dapr-ftp-binding) | `bindings.ftp` | SFTP base only; see [Gaps](#gaps-in-the-other-bindings). |
| Dapr built-in | `bindings.sftp` | The base. No extensions. |


## Component metadata

### Connection (protocol-specific)

| Field | SFTP | FTP | SMB |
|---|---|---|---|
| `address` | `host:port` | `host:port` | FQDN, optional `:port` (445). Never an IP under Kerberos. |
| `username` / `password` | yes | yes | yes (Kerberos principal or NTLM user) |
| `rootPath` | required | required | required, inside the share |
| other | `privateKey`, `privateKeyPassphrase`, `hostPublicKey`, `knownHostsFile`, `insecureIgnoreHostKey` | `tlsMode`, `insecureSkipVerify`, `disableEPSV`, `trustPASVIP`, `maxConnections` | `share`, `authMode`, `keytab`, `realm`, `kdcs`, `requireEncryption` |
| `timeout` **ext** | — | yes | yes |

`rootPath` is the working directory. Request paths are joined to it, **not
confined** to it: the server's permissions decide what the account can reach.

### Write side: completion settings **ext**

Partners must never pick up a file we have not finished writing.

| Field | Default | Values |
|---|---|---|
| `tempFileMode` | `name` | `none`: write the final name directly. `name`: write a temp name next to the target, then rename. `folder`: write into `tempFolder`, then rename into place (use when the partner picks up `*`). |
| `tempFileNamePattern` | `{name}.tmp` | Temp name for `name` mode. `{name}` is the final file name, e.g. `.{name}`, `~{name}`, `{name}.lock`. |
| `tempFolder` | `_staging` | For `folder` mode, relative to the target file's directory. Must be on the same volume so the rename stays atomic. |
| `onTargetExists` | `fail` | `fail`: never overwrite. `overwrite`: replace atomically where the protocol can. `uniqueName`: write `a_1.xml`, `a_2.xml`, … |
| `staleTempFileAge` | empty (off) | Delete our own temp files older than this (left by a crash between write and rename). Only touches names matching `tempFileNamePattern`, or files in `tempFolder`. Use **10m or more**: the age compares the server's modification time with the component's clock, which may differ by a few minutes (Kerberos tolerates 5), and a temp file of a slow upload must never look stale. |
| `completionMarkerPattern` | empty (off) | After the rename, write an empty marker, e.g. `{name}.done`, for partners that wait for one. Retried once; if it still fails, the error says the file **was written** and must not be resent. |

Order: write temp (exclusive) → close → verify size → rename to final → marker.

### Read side (input binding) **ext**

We must never read a file the partner has not finished writing.

| Field | Default | Values |
|---|---|---|
| `inboundPath` | `rootPath` | Folder to poll. |
| `filePattern` | `*` | Comma-separated globs, e.g. `*.xml,*.csv`. Case-sensitive. |
| `ignorePatterns` | empty | Comma-separated globs never picked up. Our own `tempFileNamePattern` and both marker patterns are **always** ignored too. |
| `pollInterval` | `10s` | |
| `claimMode` | `rename` | `rename`: atomically move into `processingFolder/<instance>/` before reading; safe with several replicas. `none`: read in place (single replica only). |
| `processingFolder` | `processing` | Relative to `inboundPath`. Each replica uses its own `<instance>` subfolder. |
| `requireMarkerPattern` | empty (off) | Only pick up `{name}` once e.g. `{name}.done` exists. The marker is removed with the file. |
| `stableFor` | empty (off) | Only pick up a file once size and modification time have not changed for this long. |
| `afterRead` | `move` | After the app acknowledges: `move` to `processedFolder`, `delete`, or `none` (leave it). |
| `processedFolder` | `processed` | Relative to `inboundPath`. Name collisions get `_1`, `_2`, … |
| `errorFolder` | `error` | Where a file goes when the app rejects it. Set to empty to put it back for another try instead. |
| `staleClaimAge` | `15m` | Return files claimed by a replica that has gone away (crash, rollout) to `inboundPath` once its heartbeat is this old. Set to empty to never take over. Use 10m or more. |

**A share configured with none of these behaves correctly.** The options
exist only to match a specific partner's convention, agreed per partner:
partner temp+rename (default works), or a completion marker, with
`stableFor` as a fallback.

Duration fields take a unit (`10s`, `5m`, `1h`); a bare number is rejected.


## Output operations

Request metadata keys are case-insensitive.

| Operation | Request metadata | Response data | Response metadata |
|---|---|---|---|
| `create` | `fileName` (required), `rootPath` (override), `onTargetExists` **ext** (override) | `{"fileName": "<path relative to rootPath>"}` | `fileName`, `filePath` **ext** |
| `get` | `fileName` (required), `rootPath` | the file's bytes | `fileName`, `filePath` **ext**, `size` **ext** |
| `list` | `fileName` (optional, a folder; default `rootPath`), `rootPath` | `[{"fileName", "isDirectory", "size" ext, "modTime" ext}]` | `fileName` |
| `delete` | `fileName` (required), `rootPath` | empty | — |
| `rename` **ext** | `fileName`, `newFileName` (both required, relative to `rootPath`), `onTargetExists` | `{"fileName": "<new path>"}` | `fileName`, `filePath` |

- `create` creates missing parent folders, like SFTP, and applies the write-side
  completion settings.
- The `fileName` in a `create` response is the **final** name, which differs
  from the request under `onTargetExists: uniqueName`. For a plain name it is
  what SFTP returns.
- `modTime` is RFC 3339 UTC. `list` order is the server's own.
- Every operation is advertised by `ListOperations`; the sidecar rejects any
  that is not.

## Input binding **ext**

Each claimed file is delivered as one message:

| | |
|---|---|
| data | the file's bytes |
| content type | guessed from the extension, else `application/octet-stream` |
| metadata `fileName` | the partner's file name |
| metadata `filePath` | where it is now (the processing folder) |
| metadata `size`, `modTime` | as observed |

The app's HTTP 2xx (or gRPC OK) applies `afterRead`; an error applies the
error policy. Delivery is **at least once**: a file is only moved out of the
processing folder after the app's answer, so a crash anywhere before that means
redelivery, never loss. Idempotency (name + size + hash) belongs in the
adapter's state store.

Claims that were never answered are recovered in two ways:

- **Same replica** (stream reopened, container restarted in the same pod): its
  own `processing/<instance>/` is delivered first when the stream opens.
- **Replica gone** (crash, rollout, rescheduling; Deployment pods get new
  names): every replica writes `processing/<instance>/.heartbeat` about once a
  minute. Once a folder's heartbeat is older than `staleClaimAge`, another
  replica returns its files to `inboundPath`, and they are claimed afresh. File
  times cannot be used for this: a rename keeps the partner's modification time.

Files go one at a time: the next is delivered after the app answers the
previous one.

### Ack timeout

The binding waits for the app's answer as long as it takes; it has no timeout
of its own, because redelivering a file while the app is still working on it
would process it twice. Bound it with a Dapr resiliency policy on the
component's inbound direction instead. When it expires, daprd answers the
binding with an error, which applies the error policy (`errorFolder`):

```yaml
apiVersion: dapr.io/v1alpha1
kind: Resiliency
metadata:
  name: file-adapter
spec:
  policies:
    timeouts:
      fileHandling: 5m
  targets:
    components:
      partner-share:
        inbound:
          timeout: fileHandling
```

Choose the timeout above the slowest legitimate handling time. The app may
still be working when it fires, so the adapter must stay idempotent.

## Payload encoding

Data is bytes end to end over gRPC (the Dapr SDKs). Dapr's **HTTP** binding
API JSON-encodes the `data` field, so a string sent with curl arrives quoted
and escaped. That is Dapr, not the binding, and applies to SFTP and FTP too.

## Message size

Neither the Dapr binding API nor the pluggable component protocol streams, so
a file is one gRPC message. Defaults allow 4 MiB. Raise the component's
`--max-message-size` together with the sidecar's `dapr.io/max-body-size`.
Inbound files over the limit go to `errorFolder` with a logged reason.
(Larger files need a claim-check through object storage; not in v1.)

## Errors

An error reaches the operator as one string, so it is one actionable sentence:

```
<protocol>: <operation> "<path>" on <credential-free target>: <server message> [<code>] (<explanation>)
smb: rename "out/a.xml.tmp" on smb://fs01.corp.example.com/exchange: exists [NTSTATUS 0xC0000035] (the target already exists and onTargetExists is "fail")
```

The gRPC status code is mapped so callers can branch without parsing:
`NOT_FOUND`, `ALREADY_EXISTS`, `PERMISSION_DENIED`, `UNAUTHENTICATED`,
`INVALID_ARGUMENT` (configuration or request), `UNAVAILABLE` (retry later:
file in use, connection lost), `INTERNAL`.

## Protocol differences

Same configuration, different implementation:

| | Rename onto an existing target | `overwrite` | Partner still writing |
|---|---|---|---|
| SMB | refused (`STATUS_OBJECT_NAME_COLLISION`) | atomic replace | rename refused with `STATUS_SHARING_VIOLATION`: skipped until next poll |
| SFTP | refused by standard rename | `posix-rename@openssh.com` if the server supports it | not detectable: rely on the partner's temp+rename, a marker or `stableFor` |
| FTP | server-specific (`RNFR`/`RNTO`) | delete + rename, **not atomic** | not detectable: as SFTP |

## Gaps in the other bindings

To bring `dapr-ftp-binding` to v1:

1. `create` responds with the joined server path (`/upload/hello.txt`); v1
   responds relative to `rootPath` (`hello.txt`), as SFTP does. Put the full
   path in `filePath` metadata instead.
2. `create` does not create parent folders; SFTP and v1 do.
3. No `rename`, completion settings, `list` `size`/`modTime`, or input binding.
4. `get` returns `fileName` as the full server path; v1 uses the request path.

For SFTP the built-in binding has none of the extensions, so v1 behaviour on
SFTP needs our own SFTP component (or contributing them upstream).
