# Soak test

Runs file bindings for hours on a local k3d cluster set up like a production AKS cluster,
with faults injected, to show whether Jobs and Deployments that use them can be relied on.
This binding (SMB) runs side by side with
[dapr-ftp-binding](https://github.com/integrio-intropy/dapr-ftp-binding) (FTPS), each with
its own workloads, so one run covers both.

## What runs

| Part | Setup | Copied from |
|---|---|---|
| Cluster | k3d `smb-soak`, Dapr 1.19.0 | a production cluster's Dapr version |
| SMB server | Samba AD DC: Kerberos only, SMB 3 with encryption, 1-hour tickets ([k8s/samba-dc.yaml](k8s/samba-dc.yaml)) | a test cluster's Samba DC |
| FTP server | ProFTPD from dapr-ftp-binding's integration tests, explicit TLS ([k8s/ftp-server.yaml](k8s/ftp-server.yaml)) | an FTPS server |
| Components | `bindings.smb` and `bindings.ftp`, injected containers with the production args, limits (256Mi and 128Mi) and, for SMB, probe and security context ([k8s/component.yaml](k8s/component.yaml), [k8s/ftp-component.yaml](k8s/ftp-component.yaml)) | production Components |
| Jobs | `OnFailure`, `backoffLimit: 5`, `INTROPY_JOB_SIDECAR_SHUTDOWN=OnSuccess`; 2 per binding at a time, back to back ([k8s/job-template.yaml](k8s/job-template.yaml)) | production CronJobs |
| Deployments | 2 replicas per binding, `exit-when-idle: off`, rounds back to back ([k8s/service.yaml](k8s/service.yaml), [k8s/service-ftp.yaml](k8s/service-ftp.yaml)) | production loaders |

The workload ([app/](app/)) uses Intropy.Framework 2.1.0-beta.9's `SmbAdapter` for SMB and
`SftpAdapter` for FTP (`SOAK_KIND=ftp`), and `JobRunner`, as the components do. A round writes files of 1 KB to 4.5 MB (now and then one of
16 MiB, the largest prod allows), lists them, reads each back and compares SHA-256, and deletes
them. One Job in ten fails its first attempt on purpose, to exercise the retry in the same pod.

Every 10 to 20 minutes [soak.py](soak.py) injects one fault at random, against either
binding: it kills daprd or the binding in a Job or Deployment pod, restarts the Samba or FTP
server, restarts a Deployment, or loads every CPU for 3 minutes so containers start slowly.

## Running it

```bash
k3d cluster create smb-soak --servers 1 --agents 0 --no-lb \
  --k3s-arg '--disable=traefik@server:0' --kubeconfig-update-default=false
k3d kubeconfig get smb-soak > soak/.kubeconfig
export KUBECONFIG=$PWD/soak/.kubeconfig
helm upgrade --install dapr dapr/dapr --version 1.19.0 -n dapr-system --create-namespace \
  --set global.ha.enabled=false --wait

git worktree add /tmp/binding-under-test <ref>   # the version under test
docker build -t dapr-smb-binding:0.4.1-dev /tmp/binding-under-test   # the tag k8s/component.yaml uses
docker build -t soak-samba-dc:dev tests/integration/samba-dc
docker build -t smb-soak-app:dev soak/app
# The FTP binding under test, and its test server, from a dapr-ftp-binding checkout:
docker build -t dapr-ftp-binding:soak <dapr-ftp-binding>
docker build -t soak-proftpd:1 <dapr-ftp-binding>/tests/integration/proftpd
k3d image import -c smb-soak dapr-smb-binding:0.4.1-dev soak-samba-dc:dev smb-soak-app:dev \
  dapr-ftp-binding:soak soak-proftpd:1

kubectl create ns soak
kubectl apply -f soak/k8s/samba-dc.yaml -f soak/k8s/component.yaml
kubectl rollout status sts/samba-dc -n soak
kubectl apply -f soak/k8s/ftp-server.yaml -f soak/k8s/ftp-component.yaml
kubectl apply -f soak/k8s/service.yaml -f soak/k8s/service-ftp.yaml

caffeinate -is python3 soak/soak.py run --hours 24
python3 soak/soak.py report   # any time, also mid-run
```

Results land in `soak/results/`: `jobs.csv`, `service.csv`, `chaos.csv`, `memory.csv`,
`soak.log`, and `hung/` for any Job that ran over 10 minutes. Rows carry the binding, and
the report has a section for each.

## Reading the report

- **Jobs without a fault nearby must all complete.** A Job that overlapped a fault may fail;
  that shows what a fault costs, not a defect.
- **No file mismatches**, anywhere.
- **Deployments recover within 2 minutes** of every fault, and never fail without one.
- **Binding memory** in the Deployment pods stays flat between the first and last hour.

## What it can't show

Container start times on Docker Desktop differ from AKS nodes; the CPU fault stands in for a
busy node. Azure networking, a real Windows file server, DFS and a real FTPS server behind a
firewall are not part of it.
