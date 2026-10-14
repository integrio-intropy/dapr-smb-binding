# Intropy file binding contract, v1

One contract for every file-transfer binding the Intropy FileAdapter talks to,
so the adapter is protocol-agnostic: moving a share from SMB to SFTP or FTP is a
change of `spec.type` and the connection fields, nothing else.

**The base is Dapr's built-in [SFTP binding](https://docs.dapr.io/reference/components-reference/supported-bindings/sftp/).**
Everything it defines is kept. Anything added is marked **ext**, and every
binding uses the same name and meaning for it.

v1 is deliberately small. Further options are listed under
[Reserved extensions](#reserved-extensions) with their names fixed in advance,
and are implemented when a partner or an incident needs them.

| Binding | `spec.type` | Status |
|---|---|---|
| [dapr-smb-binding](https://github.com/integrio-intropy/dapr-smb-binding) | `bindings.smb` | v1 |
| [dapr-ftp-binding](https://github.com/integrio-intropy/dapr-ftp-binding) | `bindings.ftp` | SFTP base; see [Gaps](#gaps-in-the-other-bindings) |
| Dapr built-in | `bindings.sftp` | The base, no extensions |

## Component metadata

### Connection (protocol-specific)

| | SFTP | FTP | SMB |
|---|---|---|---|
| `address` | `host:port` | `host:port` | FQDN, optional `:port`; never an IP |
| `username`, `password` | yes | yes | yes (Kerberos) |
| `rootPath` | required | required | required, inside the share |
| other | `privateKey`, `knownHostsFile`, … | `tlsMode`, `maxConnections`, … | `share`, `authMode`, `keytab`, `realm`, `kdcs`, `requireEncryption` |
| `timeout` **ext** | | yes | yes |

`rootPath` is joined to request paths, not a boundary: the server's
permissions decide what the account can reach.

### Shared extensions in v1

| Field | Default | Meaning |
|---|---|---|
| `tempFileNamePattern` | `{name}.tmp` | `create` writes this name, then renames it to the final one. Never overwrites an existing file. |
| `inboundPath` | `rootPath` | Folder the input binding polls. |
| `filePattern` | `*` | Comma-separated globs to pick up. |
| `ignorePatterns` | | Comma-separated globs never picked up. Our own temp pattern is always ignored. |
| `pollInterval` | `10s` | |
| `processedFolder` | `processed` | Inside `inboundPath`; where a file goes when the app answers OK. |
| `errorFolder` | `error` | Inside `inboundPath`; where a file goes when the app answers with an error. |

Durations take a unit (`10s`, `5m`); a bare number is rejected.

## Output operations

Request metadata keys are case-insensitive.

| Operation | Request metadata | Response data | Response metadata |
|---|---|---|---|
| `create` | `fileName` | `{"fileName": "<path relative to rootPath>"}` | `fileName`, `filePath` **ext** |
| `get` | `fileName` | the bytes | `fileName`, `size` **ext** |
| `list` | `fileName` (optional folder) | `[{"fileName", "isDirectory", "size" ext, "modTime" ext}]` | |
| `delete` | `fileName` | empty | |
| `rename` **ext** | `fileName`, `newFileName` | `{"fileName": "<new path>"}` | `fileName`, `filePath` |

`create` creates missing parent folders, as SFTP does. `create` and `rename`
fail if the target exists. `modTime` is RFC 3339 UTC.

## Input binding **ext**

One message per file: the bytes as data, a content type from the extension,
and metadata `fileName`, `size` and `modTime`.

A file is delivered only when it is complete, by each protocol's best means
(SMB: an exclusive open, refused while the partner writes). It stays in
`inboundPath` until the app answers, then moves to `processedFolder` or
`errorFolder`. A crash before that means it is delivered again: **at least
once**, so idempotency (name + size + hash) belongs in the adapter.

Files are delivered one at a time, and v1 assumes **one replica** per input
component.

**Ack timeout.** The binding waits for the app's answer as long as it takes.
Bound it with a Dapr resiliency policy on the component's inbound direction;
when it fires, daprd answers with an error and the file goes to `errorFolder`:

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

## Payload and size

Data is raw bytes over gRPC (the Dapr SDKs). Dapr's HTTP API JSON-encodes a
string `data`, so a string sent with curl arrives quoted; that is Dapr, for
every binding. A file is one gRPC message, 4 MiB by default; raise the
component's `--max-message-size` with the sidecar's `dapr.io/max-body-size`.

## Errors

One sentence, so it is useful as the only thing an operator sees:

```
smb: rename "out/a.xml.tmp" on smb://fs01.corp.example.com/exchange: ... [NTSTATUS 0xC0000035]
```

gRPC status codes let callers branch without parsing: `NOT_FOUND`,
`ALREADY_EXISTS`, `PERMISSION_DENIED`, `UNAUTHENTICATED`, `INVALID_ARGUMENT`,
`UNAVAILABLE` (retry later), `INTERNAL`.

## Reserved extensions

Not implemented in v1. The names and meanings are fixed so that whichever
binding needs one first adds it the same way.

| Field | Values | For |
|---|---|---|
| `claimMode` | `none` (v1 behaviour), `rename` | Several replicas: claim a file by renaming it into `processing/<instance>/` first. Needs recovery of claims from replicas that went away. |
| `tempFileMode` | `name` (v1), `folder`, `none` | Partners that pick up `*`: write in a staging folder, then rename into place. |
| `tempFolder` | path | Staging folder for `tempFileMode: folder`. |
| `onTargetExists` | `fail` (v1), `overwrite`, `uniqueName` | Partners that expect files to be replaced, or unique names. |
| `staleTempFileAge` | duration | Delete our own temp files left by a crash between write and rename. |
| `completionMarkerPattern` | e.g. `{name}.done` | Partners that wait for a marker after the file. |
| `requireMarkerPattern` | e.g. `{name}.done` | Partners that write a marker; only pick up a file once it exists. |
| `stableFor` | duration | Partners whose writes cannot be detected otherwise: wait until size and time stop changing. |
| `afterRead` | `move` (v1), `delete`, `none` | Partners that want files deleted rather than archived. |

## Protocol differences

| | Rename onto an existing target | Detecting a partner still writing |
|---|---|---|
| SMB | refused (`STATUS_OBJECT_NAME_COLLISION`) | exclusive open refused (`STATUS_SHARING_VIOLATION`) |
| SFTP | refused by standard rename | not detectable: the partner must write a temp name and rename |
| FTP | server-specific | not detectable: as SFTP |

## Gaps in the other bindings

To bring `dapr-ftp-binding` to v1:

1. `create` responds with the joined server path (`/upload/hello.txt`); v1
   responds relative to `rootPath` (`hello.txt`), as SFTP does.
2. `create` overwrites and does not create parent folders; v1 writes a temp
   name, renames without overwriting, and creates folders.
3. No `rename`, `list` `size`/`modTime`, or input binding.
