# Standalone

Runs the component and `daprd` next to each other against the integration
lab's Samba AD DC. Both must share the socket folder and resolve `dc.lab.test`,
so the simplest place to do this is a container on the lab's network:

```bash
make integration-up
docker build -t dapr-smb-binding:dev .
docker run --rm -it --network dapr-smb-binding-it_lab \
  -v smb-sockets:/tmp/dapr-components-sockets \
  dapr-smb-binding:dev --log-level debug
```

then, in a second container on the same network and volume:

```bash
daprd --app-id smb-demo --resources-path ./components --dapr-http-port 3500
curl -X POST localhost:3500/v1.0/bindings/smb \
  -d '{"operation":"create","data":"hello","metadata":{"fileName":"hello.txt"}}'
```
