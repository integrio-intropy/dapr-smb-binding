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
  the binding. The pod's result is the app's exit code. The app does not need
  to call `/v1.0/shutdown`.
- **Restarts.** A native sidecar is restarted whatever the pod's
  `restartPolicy`, so `Never` is fine for the Job.

Needs Kubernetes 1.29 or later. The image and arguments now live in the
workload instead of on the Component; keep them equal to the
`dapr.io/component-container` annotation if Deployments use that.

If daprd still restarts once at the start of a pod, check its log for an
`Init` timeout before suspecting the start order: see `initTimeout` in
[`component.yaml`](component.yaml).

### Do not inject the binding into a Job

With `dapr.io/inject-pluggable-components: "true"` the Dapr injector adds the
binding to `spec.containers`. A pod is only `Succeeded` once **every**
container has stopped, and nothing stops that container when the app is done,
so the Job never completes. Earlier versions of the binding could exit by
themselves for this case (`--exit-when-idle`); that was removed in favour of
the native sidecar layout above.
