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

The app receives inbound files as `POST /partner-share` (the component name)
and must answer 2xx to have the file moved to `processed/`.

## Jobs and CronJobs

A pod is only `Succeeded` once **every** container has stopped, and the Dapr
injector adds this binding to `spec.containers`. So when a Job's app finishes
and shuts the sidecar down, the binding keeps running and the Job never
completes.

Let the binding exit when the sidecar does, with this in the
`dapr.io/component-container` annotation:

```json
"args": ["--health-port", "9464", "--exit-when-idle", "30s"]
```

It watches the sidecar's HTTP port on `localhost` (3500, or `DAPR_HTTP_PORT`),
which every container in a pod shares and which daprd listens on for as long
as it runs. Once the sidecar has been up and then gone for 30 s, the binding
shuts down cleanly and exits with 0. The timer only starts once the sidecar
has been seen, and a restart within the timeout resets it.

(Not the sidecar's connection to the binding's socket: daprd closes that after
30 minutes without calls while it keeps running.)

Give Jobs a **separate Component** from the one your Deployments use, scoped to
the Jobs' app-ids. The flag lives on the container, so every pod that injects
that Component gets it, and in a Deployment an ordinary sidecar restart would
then take the binding down with it.
