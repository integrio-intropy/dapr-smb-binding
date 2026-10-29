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
binding is listening.

A Job runs with `restartPolicy: Never` and `backoffLimit: 5`. The binding
exits once the sidecar has not connected within `--exit-when-no-sidecar`, the
pod fails, and the Job retries in a fresh pod. Avoid `OnFailure`: an app that
shuts its sidecar down and then exits non-zero is restarted in a pod whose
daprd exited 0 and is never restarted, so every retry fails.

The app receives inbound files as `POST /partner-share` (the component name)
and must answer 2xx to have the file moved to `processed/`.

## Jobs and CronJobs

A pod is only `Succeeded` once **every** container has stopped, and the Dapr
injector adds this binding to `spec.containers`. So when a Job's app finishes
and shuts the sidecar down, the binding keeps running and the Job never
completes.

Run the Job with `restartPolicy: Never` and `backoffLimit: 5` (see
[Startup order](#startup-order-daprd-may-miss-the-binding)). The binding exits when
the sidecar does, because [`component.yaml`](component.yaml) turns that on for
every pod:

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
binding exits then too.

A **Deployment's pods turn it off** with this annotation on the pod template
(see [`deployment.yaml`](deployment.yaml)), so an ordinary sidecar restart
cannot take the binding down:

```yaml
smb-binding.intropy.io/exit-when-idle: "off"
```

The container in [`component.yaml`](component.yaml) maps it to
`SMB_BINDING_EXIT_WHEN_IDLE` with the downward API, and a value there overrides
`--exit-when-idle`; a pod without the annotation gets an empty value and keeps
the flag. The container spec is the Component's and the same in every pod; only
the pod annotation differs, so Jobs and Deployments share one Component.

The default is on, not off, because forgetting the annotation should be loud.
A Deployment without it exits once its sidecar has been gone for 30 s, and
restarts and alerts. A Job whose binding never exits just hangs, and with
`concurrencyPolicy: Forbid` its CronJob never runs again.
