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
