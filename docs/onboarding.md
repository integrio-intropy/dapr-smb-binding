# Onboarding a customer share

Per share, onboarding is configuration only: nothing is installed at the
customer. Work through this with the customer's network, AD and file server
admins, then with the partner who reads or writes the files.

## 1. Network

Connectivity is a site-to-site IPsec VPN by default, or Skupper if the
customer accepts a small Linux container on-prem (Skupper is TCP only; the
binding already forces Kerberos over TCP).

| From the cluster to | Port | Why |
|---|---|---|
| The file server | TCP 445 | SMB |
| At least two domain controllers | TCP 88 (UDP 88 optional) | Kerberos |

- **DNS is not required.** Set `kdcs` in the component and map the file server
  and DC names with pod `hostAliases`.
- **Addressing:** plan NAT for customer ranges that overlap ours, and give the
  customer a predictable source range (a dedicated subnet or node pool) for
  their firewall rules.

## 2. Ask the AD and share admins to verify

- [ ] SMB 3.x is enabled, and `EncryptData` is on for the share (or encryption
      is at least supported: the binding requires it by default).
- [ ] We connect with a name that has an SPN `cifs/<fqdn>`. For an alias or
      CNAME: `setspn -S cifs/<alias> <computer account>`. Never an IP address.
- [ ] AES Kerberos encryption types on the service account and on the file
      server's computer account (`msDS-SupportedEncryptionTypes`); RC4 is not
      required. Keytabs must contain AES keys.
- [ ] No "Log On To" workstation restriction on the account; Kerberos
      pre-authentication required (the default).
- [ ] The account has "Access this computer from the network" on the file
      server and is not in a "Deny access…" policy.
- [ ] Share and NTFS permissions: **Modify** on the exchange folders —
      create, write, rename and delete, in both source and target folders,
      including the `processing`, `processed` and `error` subfolders the
      binding creates (each replica also keeps a small `.heartbeat` file in
      its `processing/<pod>/` folder).
- [ ] Password rotation is agreed (in keytab mode a new keytab after every
      change), and the account lockout threshold is known.
- [ ] A direct `\\server\share` path, not DFS (or DFS is included in testing).
- [ ] A dedicated share scoped to the exchange folders, not e.g. an
      administrative `$` share covering much more.

Protected Users membership is **not** a blocker: it restricts NTLM, RC4 and
delegation, none of which the binding uses. The customer can block NTLM
entirely.

### Creating a keytab (optional)

Password mode needs nothing more. For keytab mode, on a domain-joined Windows
machine:

```powershell
ktpass /princ svc-intropy@CORP.EXAMPLE.COM /mapuser CORP\svc-intropy `
  /crypto AES256-SHA1 /ptype KRB5_NT_PRINCIPAL /pass * /out svc-intropy.keytab
```

`ktpass` with `/pass` resets the password; coordinate with the rotation
process. Store `base64 -w0 svc-intropy.keytab` in the secret.

## 3. Agree the completion convention with the partner

The binding's defaults work when the partner does **either** of these:

| Partner writes by… | Configure |
|---|---|
| Writing a temp name and renaming (most tools, BizTalk "use temporary file") | nothing |
| Writing in place, keeping the file open until done (normal Windows apps) | nothing: the claim fails with a sharing violation until they close |
| Writing in place in several opens, or from a non-Windows client | `requireMarkerPattern: "{name}.done"` if they can write a marker, else `stableFor: 30s` |

For files we write:

| Partner picks up… | Configure |
|---|---|
| A specific pattern, e.g. `*.xml` | nothing: we write `name.xml.tmp` and rename |
| Everything (`*`) | `tempFileMode: folder` |
| Only after a marker | `completionMarkerPattern: "{name}.done"` |

Agree also what happens when a file of the same name already exists
(`onTargetExists`: `fail` by default) and who cleans up `processed/`.

## 4. Configure and verify

1. Create the secret and component (see [examples/kubernetes](../examples/kubernetes)).
2. Check the component container log for `output binding ready` and
   `input binding ready`. An authentication problem fails `Init` with the
   reason (wrong password, clock skew, no SPN, KDC unreachable…), and the
   binding backs off before retrying to avoid locking the account.
3. `create` a test file, have the partner confirm they see it complete and only
   once; have them drop a file and confirm it reaches the app and moves to
   `processed/`.

## Troubleshooting

| Error contains | Meaning |
|---|---|
| `Server not found in Kerberos database` | No SPN for `address`. Use the FQDN in AD, or `setspn`. |
| `Clock skew too great` | Node clock and DC differ by more than 5 minutes. |
| `Cannot contact any KDC` | TCP 88 blocked, or wrong `kdcs`. |
| `Preauthentication failed` | Wrong password or stale keytab. |
| `0xC0000418` (NTLM blocked) | `authMode: ntlm` against a server that refuses NTLM. Use Kerberos. |
| `0xC00000CC` (bad network name) | The share does not exist. |
| `0xC0000022` (access denied) | Share or NTFS permissions. |
| `0xC0000043` (sharing violation) on `get`/`delete` | Someone has the file open. Inbound polling handles this by itself. |
| `0xC0000035` (name collision) | The target exists and `onTargetExists` is `fail`. |
