# Kubernetes

1. Create the secret (password, or base64 keytab):

   ```bash
   kubectl create secret generic partner-smb --from-literal=password='...'
   # keytab mode:
   kubectl create secret generic partner-smb --from-literal=keytab="$(base64 -w0 svc-intropy.keytab)"
   ```

   Prefer syncing it from Key Vault with External Secrets Operator.

2. Apply [`component.yaml`](component.yaml) and [`deployment.yaml`](deployment.yaml).
   The sidecar injector adds the `smb-binding` container to the pod; the pod
   must define the `smb-binding-runtime` volume it mounts.

3. Check the component registered:

   ```bash
   kubectl logs deploy/file-adapter -c smb-binding
   kubectl logs deploy/file-adapter -c daprd | grep -i smb
   ```

## Startup order: daprd may miss the binding

daprd looks for component sockets once, about 20 ms after it starts, and the
injector starts this binding after daprd. When the binding is not listening
yet, daprd exits with `couldn't find binding`. A Deployment
(`restartPolicy: Always`) restarts daprd in the same pod, and by then the
binding is listening. That costs one daprd restart when a pod starts, nothing
more.

In a Job the same race is worse, which is why a Job runs the binding as a
native sidecar instead (next section).

The app receives inbound files as `POST /partner-share` (the component name)
and must answer 2xx to have the file moved to `processed/`.

## Jobs and CronJobs

Run the binding and daprd as Kubernetes native sidecars: init containers with
`restartPolicy: Always`. [`cronjob.yaml`](cronjob.yaml) is a complete example.

What the pod needs:

1. The binding declared under `initContainers` with `restartPolicy: Always`
   and a startup probe on `/healthz`.
2. `dapr.io/enable-native-sidecar: "true"`, so Dapr injects daprd as a native
   sidecar. The injector appends daprd to `initContainers`, after the binding.
3. `dapr.io/inject-pluggable-components: "false"`, because the pod brings the
   binding itself.
4. An `emptyDir` volume for the socket folder, mounted in the binding and
   given to daprd with `dapr.io/volume-mounts`, next to the
   `smb-binding-runtime` volume.

The kubelet then does the rest:

- **Start order.** daprd starts only when the binding's startup probe has
  passed, so it finds the socket on its first start.
- **Completion.** When the app container exits, the kubelet stops daprd and
  the binding. The pod's result is the app's exit code. The binding needs no
  `--exit-when-idle`, and the app does not need to call `/v1.0/shutdown`.
- **Restarts.** A native sidecar is restarted whatever the pod's
  `restartPolicy`, so `Never` is fine for the Job.

Needs Kubernetes 1.29 or later. The image and arguments now live in the
workload instead of on the Component; keep them equal to the
`dapr.io/component-container` annotation if Deployments use that.

If daprd still restarts once at the start of a pod, check its log for an
`Init` timeout before suspecting the start order: see `initTimeout` in
[`component.yaml`](component.yaml).

### If the binding is injected into a Job instead

Use this only where native sidecars are not available.

A pod is only `Succeeded` once **every** container has stopped, and the Dapr
injector adds this binding to `spec.containers`. So when a Job's app finishes
and shuts the sidecar down, the binding keeps running and the Job never
completes.

Run the Job with `restartPolicy: OnFailure` and `backoffLimit: 5`, so that a
daprd that missed the binding's socket is restarted in the same pod. Don't use
`Never` here: every retry is a fresh pod where daprd starts before the binding
again. `OnFailure` needs an app that leaves daprd running when it exits
non-zero: a daprd that was shut down exits 0, Kubernetes does not restart a
container that exited 0, and the app's retry then waits for a sidecar that
never comes back (Intropy.Framework: `INTROPY_JOB_SIDECAR_SHUTDOWN=OnSuccess`,
2.1.0-beta.9 or later).

Let the binding exit when the sidecar does, with these arguments in the
`dapr.io/component-container` annotation:

```json
"args": ["--health-port", "9464", "--exit-when-idle", "30s", "--exit-when-no-sidecar", "30s"]
```

The sidecar counts as seen from its first `Init` on the binding's socket, and
as running while it is connected to that socket or listening on its HTTP port
on `localhost` (3500, or `DAPR_HTTP_PORT`). Neither is enough alone: daprd
opens the port only once every component has loaded, so a Job with nothing to
do can shut it down within a second, and it closes the socket connection after
30 minutes without calls while it keeps running. Once the sidecar has been seen
and then gone for 30 s, the binding shuts down cleanly and exits with 0. A
restart within the timeout resets the timer. A sidecar that never connects
within `--exit-when-no-sidecar` has died before reaching the binding, and the
binding exits then too, so the pod fails instead of hanging.

The arguments are the Component's, so every pod that injects it gets them. A
**Deployment's pods must turn idle exit off**, or an ordinary sidecar restart
takes the binding down. Map a pod annotation to `SMB_BINDING_EXIT_WHEN_IDLE`
in the same `dapr.io/component-container` annotation:

```json
"env": [{
  "name": "SMB_BINDING_EXIT_WHEN_IDLE",
  "valueFrom": { "fieldRef": { "fieldPath": "metadata.annotations['smb-binding.intropy.io/exit-when-idle']" } }
}]
```

and set it on the Deployment's pod template:

```yaml
smb-binding.intropy.io/exit-when-idle: "off"
```

A value there overrides `--exit-when-idle`; a pod without the annotation gets
an empty value and keeps the flag. [`component.yaml`](component.yaml) and
[`deployment.yaml`](deployment.yaml) here do not carry this wiring.
