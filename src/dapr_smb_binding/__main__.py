"""Host the SMB binding as a Dapr pluggable component on a Unix domain socket."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
from concurrent import futures

import grpc
from grpc_reflection.v1alpha import reflection

from . import __version__, metrics
from ._proto import bindings_pb2 as pb
from ._proto import bindings_pb2_grpc as pb_grpc
from .config import Config
from .fs import FileSystem
from .server import InputService, OutputService, Registry

log = logging.getLogger("dapr_smb_binding")

# The sidecar reads DAPR_COMPONENTS_SOCKETS_FOLDER; the SDKs have used other
# spellings. Honour all of them, preferring the sidecar's (see the FTP binding).
SOCKET_FOLDER_ENVS = (
    "DAPR_COMPONENTS_SOCKETS_FOLDER",
    "DAPR_COMPONENT_SOCKETS_FOLDER",
    "DAPR_COMPONENT_SOCKET_FOLDER",
)
DEFAULT_SOCKET_FOLDER = "/tmp/dapr-components-sockets"  # noqa: S108 - the Dapr convention

# 4 MiB of payload plus headroom, matching the sidecar's default body limit.
DEFAULT_MAX_MESSAGE_SIZE = (4 << 20) + (1 << 20)
SHUTDOWN_GRACE = 10.0


def socket_folder() -> str:
    for env in SOCKET_FOLDER_ENVS:
        if os.environ.get(env):
            return os.environ[env]
    return DEFAULT_SOCKET_FOLDER


def parse_perm(s: str) -> int:
    """Permission bits, always read as octal ("660" is 0o660, not decimal 660)."""
    try:
        n = int(s.removeprefix("0o"), 8)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{s!r} is not an octal mode, for example "0660"') from None
    if n > 0o777:
        raise argparse.ArgumentTypeError(f"mode {n:#o} is wider than 0777")
    return n


class JsonFormatter(logging.Formatter):
    _EXTRA = ("component", "target", "operation")

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "scope": record.name,
            "msg": record.getMessage(),
        }
        for k in self._EXTRA:
            if hasattr(record, k):
                out[k] = getattr(record, k)
        if record.exc_info:
            out["error"] = self.formatException(record.exc_info)
        return json.dumps(out)


def setup_logging(level: str, fmt: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    lvl = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(level.lower())
    root.setLevel(lvl or logging.INFO)
    # smbprotocol logs every SMB message at INFO.
    for noisy in ("smbprotocol", "smbclient", "spnego"):
        logging.getLogger(noisy).setLevel(
            logging.WARNING if (lvl or logging.INFO) > logging.DEBUG else logging.INFO
        )
    if lvl is None:
        log.warning("unrecognised log level %r, using info", level)


def default_instance_id() -> str:
    return os.environ.get("POD_NAME") or os.environ.get("HOSTNAME") or socket.gethostname()


def smb_factory(cfg: Config, name: str) -> FileSystem:
    from .smb import SmbFileSystem

    return SmbFileSystem(cfg, component=name)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smb-binding", description=__doc__)
    p.add_argument(
        "--socket-name",
        default="smb",
        help="socket file name without .sock; determines spec.type (bindings.<name>)",
    )
    p.add_argument("--socket-perm", default="0660", type=parse_perm, help="socket permission bits, in octal")
    p.add_argument(
        "--max-message-size",
        default=DEFAULT_MAX_MESSAGE_SIZE,
        type=int,
        help="maximum gRPC message in bytes; raise alongside the sidecar's dapr.io/max-body-size",
    )
    p.add_argument(
        "--instance-id",
        default=default_instance_id(),
        help="names this replica's processing folder (default: POD_NAME, then HOSTNAME)",
    )
    p.add_argument(
        "--metrics-port", default=0, type=int, help="serve /metrics and /healthz on this port; 0 = off"
    )
    p.add_argument("--log-level", default="info", help="debug, info, warn or error")
    p.add_argument("--log-format", default="text", choices=("text", "json"))
    p.add_argument("--version", action="store_true", help="print the version and exit")
    args = p.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    setup_logging(args.log_level, args.log_format)
    return run(args)


def run(args: argparse.Namespace) -> int:
    folder = socket_folder()
    path = os.path.join(folder, f"{args.socket_name}.sock")
    os.makedirs(folder, mode=0o750, exist_ok=True)
    try:
        os.remove(path)  # a socket left by a previous run would make bind fail
    except FileNotFoundError:
        pass

    shutdown = threading.Event()
    reg = Registry(smb_factory, instance_id=args.instance_id, max_message_size=args.max_message_size)

    server = grpc.server(
        # Every open input stream holds a thread for its life, so leave room.
        futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="grpc"),
        options=[
            ("grpc.max_receive_message_length", args.max_message_size),
            ("grpc.max_send_message_length", args.max_message_size),
        ],
    )
    pb_grpc.add_OutputBindingServicer_to_server(OutputService(reg), server)
    pb_grpc.add_InputBindingServicer_to_server(InputService(reg, shutdown), server)
    # REQUIRED: the sidecar discovers what a pluggable component implements via
    # gRPC reflection (v1alpha). Without it the socket appears and the sidecar
    # still reports "couldn't find binding".
    reflection.enable_server_reflection(
        (
            pb.DESCRIPTOR.services_by_name["OutputBinding"].full_name,
            pb.DESCRIPTOR.services_by_name["InputBinding"].full_name,
            reflection.SERVICE_NAME,
        ),
        server,
    )

    old_umask = os.umask(0o117)  # create the socket as 0660, not briefly wider
    try:
        server.add_insecure_port(f"unix://{path}")
        server.start()
    finally:
        os.umask(old_umask)
    try:
        os.chmod(path, args.socket_perm)
    except OSError as e:
        log.warning("could not set permissions on %s: %s", path, e)

    if args.metrics_port:
        metrics.serve(args.metrics_port, reg.health)
        log.info("metrics and /healthz on :%d", args.metrics_port)

    log.info(
        "dapr-smb-binding %s listening on %s (spec.type: bindings.%s, max message size %d bytes, instance %s)",
        __version__,
        path,
        args.socket_name,
        args.max_message_size,
        args.instance_id,
    )

    def on_signal(signum, _frame):
        log.info("received %s, shutting down", signal.Signals(signum).name)
        shutdown.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    shutdown.wait()

    server.stop(SHUTDOWN_GRACE).wait()
    reg.close_all()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    log.info("shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
