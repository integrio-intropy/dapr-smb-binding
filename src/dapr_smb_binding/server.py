"""gRPC services for the Dapr pluggable component protocol.

The sidecar tells component instances apart by the x-component-instance gRPC
metadata (the component's name), so one process serves every SMB component in
the pod. Input and output are separate instances, each with its own Init.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import grpc

from ._proto import bindings_pb2 as pb
from ._proto import bindings_pb2_grpc as pb_grpc
from ._proto import common_pb2
from .config import Config, ConfigError, parse
from .errors import Kind, SmbError
from .fs import FileSystem
from .inbound import Inbound
from .output import OPERATIONS, OutputBinding

log = logging.getLogger(__name__)

# Room for protobuf framing and metadata between a file and the message limit.
MESSAGE_OVERHEAD = 64 * 1024

_STATUS = {
    Kind.NOT_FOUND: grpc.StatusCode.NOT_FOUND,
    Kind.EXISTS: grpc.StatusCode.ALREADY_EXISTS,
    Kind.ACCESS_DENIED: grpc.StatusCode.PERMISSION_DENIED,
    Kind.AUTH: grpc.StatusCode.UNAUTHENTICATED,
    Kind.INVALID: grpc.StatusCode.INVALID_ARGUMENT,
    Kind.SHARING_VIOLATION: grpc.StatusCode.UNAVAILABLE,
    Kind.RECONNECT: grpc.StatusCode.UNAVAILABLE,
}


def _name(context: grpc.ServicerContext) -> str:
    return dict(context.invocation_metadata() or ()).get("x-component-instance") or "default"


def _abort(context: grpc.ServicerContext, err: Exception) -> None:
    if isinstance(err, ConfigError):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
    code = (
        _STATUS.get(err.kind, grpc.StatusCode.INTERNAL)
        if isinstance(err, SmbError)
        else grpc.StatusCode.INTERNAL
    )
    context.abort(code, str(err))


@dataclass
class Instance:
    cfg: Config
    fs: FileSystem
    output: OutputBinding | None = None
    inbound: Inbound | None = None
    stream_open: bool = False
    stream_was_open: bool = False
    stop: threading.Event = field(default_factory=threading.Event)


class Registry:
    def __init__(self, fs_factory: Callable[[Config, str], FileSystem], *, max_message_size: int):
        self.fs_factory = fs_factory
        self.max_message_size = max_message_size
        self._lock = threading.Lock()
        self.outputs: dict[str, Instance] = {}
        self.inputs: dict[str, Instance] = {}

    def init(self, kind: str, name: str, props: dict[str, str]) -> None:
        cfg, warns = parse(props)
        for w in warns:
            log.warning("%s: %s", name, w)
        fs = self.fs_factory(cfg, name)
        try:
            fs.ping()  # bad address, share or credentials fail Init, as in the SFTP binding
        except Exception:
            fs.close()
            raise
        inst = Instance(cfg, fs)
        if kind == "output":
            inst.output = OutputBinding(cfg, fs)
        else:
            inst.inbound = Inbound(cfg, fs, max_file_size=self.max_message_size - MESSAGE_OVERHEAD)
        table = self.outputs if kind == "output" else self.inputs
        with self._lock:
            old, table[name] = table.get(name), inst
        if old:  # Init again (hot reload): retire the previous instance
            old.stop.set()
            old.fs.close()
        log.info(
            "%s: %s binding ready on %s (rootPath=%r inboundPath=%r)",
            name,
            kind,
            cfg.target,
            cfg.root_path,
            cfg.inbound_path,
        )

    def health(self) -> tuple[bool, str]:
        """Unhealthy once an input stream has closed: the sidecar never reopens it, a restart does."""
        broken = [n for n, i in self.inputs.items() if i.stream_was_open and not i.stream_open]
        return (False, "input stream closed: " + ", ".join(broken)) if broken else (True, "ok")

    def close_all(self) -> None:
        with self._lock:
            instances = [*self.outputs.values(), *self.inputs.values()]
            self.outputs, self.inputs = {}, {}
        for i in instances:
            i.stop.set()
            i.fs.close()


class OutputService(pb_grpc.OutputBindingServicer):
    def __init__(self, reg: Registry):
        self.reg = reg

    def _get(self, context) -> Instance:
        inst = self.reg.outputs.get(_name(context))
        if inst is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"smb: output binding {_name(context)!r} is not initialised",
            )
        return inst

    def Init(self, request, context):  # noqa: N802
        try:
            self.reg.init("output", _name(context), dict(request.metadata.properties))
        except (ConfigError, SmbError) as e:
            log.error("%s: init failed: %s", _name(context), e)
            _abort(context, e)
        return pb.OutputBindingInitResponse()

    def Invoke(self, request, context):  # noqa: N802
        inst = self._get(context)
        try:
            r = inst.output.invoke(request.operation, request.data, dict(request.metadata))
        except SmbError as e:
            log.warning("%s: %s", _name(context), e)
            _abort(context, e)
        return pb.InvokeResponse(data=r.data, metadata=r.metadata, content_type=r.content_type)

    def ListOperations(self, request, context):  # noqa: N802
        # The sidecar rejects any operation not listed here.
        return pb.ListOperationsResponse(operations=list(OPERATIONS))

    def Ping(self, request, context):  # noqa: N802
        try:
            self._get(context).fs.ping()
        except SmbError as e:
            _abort(context, e)
        return common_pb2.PingResponse()


class InputService(pb_grpc.InputBindingServicer):
    def __init__(self, reg: Registry, shutdown: threading.Event):
        self.reg = reg
        self.shutdown = shutdown

    def _get(self, context) -> Instance:
        inst = self.reg.inputs.get(_name(context))
        if inst is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"smb: input binding {_name(context)!r} is not initialised",
            )
        return inst

    def Init(self, request, context):  # noqa: N802
        try:
            self.reg.init("input", _name(context), dict(request.metadata.properties))
        except (ConfigError, SmbError) as e:
            log.error("%s: init failed: %s", _name(context), e)
            _abort(context, e)
        return pb.InputBindingInitResponse()

    def Ping(self, request, context):  # noqa: N802
        inst = self._get(context)
        try:
            inst.fs.ping()
        except SmbError as e:
            _abort(context, e)
        return common_pb2.PingResponse()

    def Read(self, request_iterator, context) -> Iterator[pb.ReadResponse]:  # noqa: N802
        inst = self._get(context)
        yield from _read_loop(_name(context), inst, request_iterator, context, self.shutdown)


def _read_loop(
    name: str, inst: Instance, requests, context, shutdown: threading.Event
) -> Iterator[pb.ReadResponse]:
    """Deliver one file at a time, and move it only after the sidecar relays the app's answer."""
    acks: queue.Queue = queue.Queue()
    closed = threading.Event()

    def read_acks() -> None:
        try:
            for req in requests:
                acks.put(req)
        except Exception as e:  # noqa: BLE001
            log.info("%s: read stream ended: %s", name, e)
        closed.set()
        acks.put(None)

    threading.Thread(target=read_acks, name=f"acks-{name}", daemon=True).start()

    def stopping() -> bool:
        return closed.is_set() or shutdown.is_set() or inst.stop.is_set() or not context.is_active()

    def safely(fn):
        # The sidecar never reopens a stream that ends, so errors here are logged and
        # retried on the next poll instead of ending it.
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.error("%s: %s", name, e)
            return None

    inst.stream_open = inst.stream_was_open = True
    log.info(
        "%s: read stream open, polling %r every %ss", name, inst.cfg.inbound_path, inst.cfg.poll_interval
    )
    try:
        while not stopping():
            delivered = False
            for entry in safely(inst.inbound.poll) or []:
                msg = safely(lambda e=entry: inst.inbound.load(e))
                if msg is None:
                    continue
                mid = uuid.uuid4().hex
                yield pb.ReadResponse(
                    data=msg.data, metadata=msg.metadata, content_type=msg.content_type, message_id=mid
                )
                ack = _wait_ack(acks, mid, stopping)
                if ack is None:
                    # The file is still in inboundPath and is delivered again later.
                    log.warning("%s: stream closed before %r was answered", name, msg.name)
                    return
                ok = not ack.HasField("response_error")
                safely(
                    lambda m=msg, ok=ok, a=ack: inst.inbound.complete(
                        m.name, ok=ok, error=a.response_error.message
                    )
                )
                delivered = True
                if stopping():
                    return
            # Poll again straight away after a delivery; otherwise wait. Without the wait,
            # a file the partner keeps open would make this loop poll the share flat out.
            if not delivered:
                _sleep(inst.cfg.poll_interval, stopping)
    finally:
        inst.stream_open = False
        log.info("%s: read stream closed", name)


def _wait_ack(acks: queue.Queue, mid: str, stopping: Callable[[], bool]):
    while True:
        try:
            req = acks.get(timeout=1.0)
        except queue.Empty:
            if stopping():
                return None
            continue
        if req is None:
            return None
        if req.message_id == mid:
            return req


def _sleep(seconds: float, stopping: Callable[[], bool]) -> None:
    deadline = seconds
    while deadline > 0 and not stopping():
        threading.Event().wait(min(0.25, deadline))
        deadline -= 0.25
