"""Host the SMB binding as a Dapr pluggable component on a Unix domain socket."""

from __future__ import annotations

import argparse
import http.server
import logging
import os
import signal
import sys
import threading
from concurrent import futures

import grpc
from grpc_reflection.v1alpha import reflection

from . import __version__, idle
from ._proto import bindings_pb2 as pb
from ._proto import bindings_pb2_grpc as pb_grpc
from .config import Config, ConfigError, parse_duration
from .fs import FileSystem
from .server import InputService, OutputService, Registry

log = logging.getLogger("dapr_smb_binding")

# The folder the sidecar scans for component sockets, as the injector passes it
# to component containers.
SOCKET_FOLDER_ENV = "DAPR_COMPONENT_SOCKETS_FOLDER"
DEFAULT_SOCKET_FOLDER = "/tmp/dapr-components-sockets"  # noqa: S108 - the Dapr convention
# 4 MiB of payload plus headroom, matching the sidecar's default body limit.
DEFAULT_MAX_MESSAGE_SIZE = (4 << 20) + (1 << 20)


def smb_factory(cfg: Config, name: str) -> FileSystem:
    from .smb import SmbFileSystem

    return SmbFileSystem(cfg, component=name)


def serve_health(port: int, reg: Registry) -> None:
    """/healthz for a liveness probe: the sidecar never reopens a broken input stream, a restart does."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ok, detail = reg.health() if self.path == "/healthz" else (False, "not found")
            self.send_response(200 if ok else (503 if self.path == "/healthz" else 404))
            self.end_headers()
            self.wfile.write(detail.encode() + b"\n")

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - probes reach the pod IP
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def duration(s: str) -> float:
    try:
        return parse_duration("duration", s, 0.0)
    except ConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="smb-binding",
        description=__doc__,
        epilog=f"{idle.TIMEOUT_ENV}: the pod's override of --exit-when-idle, e.g. 30s, or {idle.OFF}; "
        "set it to off for a Deployment's pod.",
    )
    p.add_argument(
        "--socket-name", default="smb", help="socket file name without .sock; spec.type is bindings.<name>"
    )
    p.add_argument(
        "--max-message-size",
        type=int,
        default=DEFAULT_MAX_MESSAGE_SIZE,
        help="maximum gRPC message in bytes; raise with the sidecar's dapr.io/max-body-size",
    )
    p.add_argument("--health-port", type=int, default=0, help="serve /healthz on this port; 0 = off")
    p.add_argument(
        "--exit-when-idle",
        type=duration,
        default=0.0,
        help="exit once the sidecar has been up and then gone this long, e.g. 30s; the Component's "
        f"default, which a pod overrides with {idle.TIMEOUT_ENV}",
    )
    p.add_argument(
        "--exit-when-no-sidecar",
        type=duration,
        default=60.0,
        help="with idle exit on, also exit if the sidecar never connects within this long, which is "
        "what happens when daprd dies before reaching us; 0 waits forever",
    )
    p.add_argument("--log-level", default="info", choices=("debug", "info", "warning", "error"))
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # smbprotocol logs every SMB message at INFO.
    for noisy in ("smbprotocol", "smbclient", "spnego"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        exit_when_idle = idle.timeout(args.exit_when_idle)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    folder = os.environ.get(SOCKET_FOLDER_ENV) or DEFAULT_SOCKET_FOLDER
    path = os.path.join(folder, f"{args.socket_name}.sock")
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        os.remove(path)  # left by a previous run

    shutdown = threading.Event()
    reg = Registry(smb_factory, max_message_size=args.max_message_size)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=16),
        options=[
            ("grpc.max_receive_message_length", args.max_message_size),
            ("grpc.max_send_message_length", args.max_message_size),
        ],
    )
    pb_grpc.add_OutputBindingServicer_to_server(OutputService(reg), server)
    pb_grpc.add_InputBindingServicer_to_server(InputService(reg, shutdown), server)
    # Required: the sidecar discovers the services through gRPC reflection.
    services = pb.DESCRIPTOR.services_by_name
    reflection.enable_server_reflection(
        (services["OutputBinding"].full_name, services["InputBinding"].full_name, reflection.SERVICE_NAME),
        server,
    )
    old_umask = os.umask(0o117)  # the socket is created 0660; the image shares daprd's uid/gid
    try:
        server.add_insecure_port(f"unix://{path}")
        server.start()
    finally:
        os.umask(old_umask)
    if args.health_port:
        serve_health(args.health_port, reg)
    if exit_when_idle:
        threading.Thread(
            target=idle.watch,
            args=(exit_when_idle, shutdown, reg.started, lambda: idle.sidecar_running(path)),
            kwargs={"start_wait": args.exit_when_no_sidecar},
            daemon=True,
        ).start()
    log.info(
        "dapr-smb-binding %s listening on %s (spec.type bindings.%s)", __version__, path, args.socket_name
    )

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: shutdown.set())
    shutdown.wait()
    log.info("shutting down")
    server.stop(10).wait()
    reg.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
