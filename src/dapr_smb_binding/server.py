"""gRPC services for the Dapr pluggable component protocol.

The sidecar tells component instances apart with the x-component-instance
gRPC metadata (the component's name), so one process serves every SMB
component in the pod. Input and output are separate instances with separate
Init calls, even for the same component name.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import grpc

from . import metrics
from ._proto import bindings_pb2 as pb
from ._proto import bindings_pb2_grpc as pb_grpc
from ._proto import common_pb2
from .config import Config, ConfigError, parse
from .errors import Kind, SmbError
from .fs import FileSystem
from .inbound import Claimed, Inbound
from .output import OPERATIONS, OutputBinding

log = logging.getLogger(__name__)

INSTANCE_ID_KEY = "x-component-instance"
DEFAULT_INSTANCE = "__default__"
# Headroom between --max-message-size and the largest inbound file, for
# protobuf framing and metadata.
MESSAGE_OVERHEAD = 64 * 1024

FileSystemFactory = Callable[[Config, str], FileSystem]


def _instance(context: grpc.ServicerContext) -> str:
    for k, v in context.invocation_metadata() or ():
        if k == INSTANCE_ID_KEY and v:
            return v
    return DEFAULT_INSTANCE


def _abort(context: grpc.ServicerContext, err: Exception) -> None:
    code = grpc.StatusCode.INTERNAL
    if isinstance(err, ConfigError):
        code = grpc.StatusCode.INVALID_ARGUMENT
    elif isinstance(err, SmbError):
        code = {
            Kind.NOT_FOUND: grpc.StatusCode.NOT_FOUND,
            Kind.EXISTS: grpc.StatusCode.ALREADY_EXISTS,
            Kind.ACCESS_DENIED: grpc.StatusCode.PERMISSION_DENIED,
            Kind.AUTH: grpc.StatusCode.UNAUTHENTICATED,
            Kind.CONFIG: grpc.StatusCode.INVALID_ARGUMENT,
            Kind.SHARING_VIOLATION: grpc.StatusCode.UNAVAILABLE,
            Kind.RECONNECT: grpc.StatusCode.UNAVAILABLE,
        }.get(err.kind, grpc.StatusCode.INTERNAL)
    context.abort(code, str(err))


@dataclass
class _Output:
    cfg: Config
    fs: FileSystem
    binding: OutputBinding


@dataclass
class _Input:
    cfg: Config
    fs: FileSystem
    inbound: Inbound
    stream_open: bool = False
    stream_ever_opened: bool = False
    stop: threading.Event = field(default_factory=threading.Event)


class Registry:
    """One instance per (direction, component name); closed on re-Init and shutdown."""

    def __init__(self, fs_factory: FileSystemFactory, *, instance_id: str, max_message_size: int):
        self.fs_factory = fs_factory
        self.instance_id = instance_id
        self.max_message_size = max_message_size
        self._lock = threading.Lock()
        self.outputs: dict[str, _Output] = {}
        self.inputs: dict[str, _Input] = {}

    def _build(self, props: dict[str, str], name: str, folder: str) -> tuple[Config, FileSystem]:
        cfg, warns = parse(props)
        for w in warns:
            log.warning("%s: %s", name, w)
        fs = self.fs_factory(cfg, name)
        try:
            # Fail at startup on a bad address, share or credentials, like the SFTP
            # binding does, rather than at the first request.
            fs.ping()
        except Exception:
            fs.close()
            raise
        path = cfg.read.inbound_path if folder == "inboundPath" else cfg.root_path
        try:
            if not fs.stat(path).is_dir:
                log.warning("%s: %s %r is a file, not a folder", name, folder, path)
        except SmbError as e:
            # Not fatal: create makes missing folders, and the folder may be created later.
            log.warning("%s: %s %r is not usable yet: %s", name, folder, path, e)
        return cfg, fs

    def init_output(self, name: str, props: dict[str, str]) -> None:
        cfg, fs = self._build(props, name, "rootPath")
        with self._lock:
            old = self.outputs.get(name)
            self.outputs[name] = _Output(cfg, fs, OutputBinding(cfg, fs, component=name))
        if old:
            old.fs.close()
        log.info(
            "%s: output binding ready: target=%s rootPath=%r authMode=%s tempFileMode=%s onTargetExists=%s",
            name,
            cfg.target,
            cfg.root_path,
            cfg.auth_mode,
            cfg.write.temp_file_mode,
            cfg.write.on_target_exists,
        )

    def init_input(self, name: str, props: dict[str, str]) -> None:
        cfg, fs = self._build(props, name, "inboundPath")
        inbound = Inbound(
            fs,
            cfg.read,
            cfg.write,
            instance_id=self.instance_id,
            max_file_size=self.max_message_size - MESSAGE_OVERHEAD,
            labels={"component": name, "target": cfg.target},
        )
        inp = _Input(cfg, fs, inbound)
        inbound.start_heartbeat(inp.stop)
        with self._lock:
            old = self.inputs.get(name)
            self.inputs[name] = inp
        if old:
            old.stop.set()
            old.fs.close()
        log.info(
            "%s: input binding ready: target=%s inboundPath=%r filePattern=%s claimMode=%s afterRead=%s "
            "pollInterval=%ss processing=%r",
            name,
            cfg.target,
            cfg.read.inbound_path,
            ",".join(cfg.read.file_patterns),
            cfg.read.claim_mode,
            cfg.read.after_read,
            cfg.read.poll_interval,
            inbound.processing_dir,
        )

    def health(self) -> tuple[bool, str]:
        with self._lock:
            broken = [n for n, i in self.inputs.items() if i.stream_ever_opened and not i.stream_open]
        if broken:
            return False, "input stream closed and not reopened by the sidecar: " + ", ".join(broken)
        return True, "ok"

    def close_all(self) -> None:
        with self._lock:
            outs, ins = list(self.outputs.items()), list(self.inputs.items())
            self.outputs, self.inputs = {}, {}
        for name, i in ins:
            i.stop.set()
            _close(name, i.fs)
        for name, o in outs:
            _close(name, o.fs)


def _close(name: str, fs: FileSystem) -> None:
    try:
        fs.close()
    except Exception as e:  # noqa: BLE001
        log.warning("%s: closing: %s", name, e)


class OutputService(pb_grpc.OutputBindingServicer):
    def __init__(self, reg: Registry):
        self.reg = reg

    def _get(self, context: grpc.ServicerContext) -> _Output:
        name = _instance(context)
        o = self.reg.outputs.get(name)
        if o is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, f"smb: output binding {name!r} is not initialised"
            )
        return o

    def Init(self, request, context):  # noqa: N802
        name = _instance(context)
        try:
            self.reg.init_output(name, dict(request.metadata.properties))
        except (ConfigError, SmbError) as e:
            log.error("%s: output init failed: %s", name, e)
            _abort(context, e)
        return pb.OutputBindingInitResponse()

    def Invoke(self, request, context):  # noqa: N802
        o = self._get(context)
        try:
            r = o.binding.invoke(request.operation, request.data, dict(request.metadata))
        except SmbError as e:
            log.warning("%s", e, extra={"component": _instance(context), "operation": request.operation})
            _abort(context, e)
        return pb.InvokeResponse(data=r.data, metadata=r.metadata, content_type=r.content_type)

    def ListOperations(self, request, context):  # noqa: N802
        # The sidecar rejects any operation not listed here.
        return pb.ListOperationsResponse(operations=list(OPERATIONS))

    def Ping(self, request, context):  # noqa: N802
        o = self._get(context)
        try:
            o.fs.ping()
        except SmbError as e:
            _abort(context, e)
        return common_pb2.PingResponse()


class InputService(pb_grpc.InputBindingServicer):
    def __init__(self, reg: Registry, shutdown: threading.Event):
        self.reg = reg
        self.shutdown = shutdown

    def Init(self, request, context):  # noqa: N802
        name = _instance(context)
        try:
            self.reg.init_input(name, dict(request.metadata.properties))
        except (ConfigError, SmbError) as e:
            log.error("%s: input init failed: %s", name, e)
            _abort(context, e)
        return pb.InputBindingInitResponse()

    def Ping(self, request, context):  # noqa: N802
        name = _instance(context)
        i = self.reg.inputs.get(name)
        if i is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, f"smb: input binding {name!r} is not initialised"
            )
        if i.stream_ever_opened and not i.stream_open:
            context.abort(grpc.StatusCode.UNAVAILABLE, "smb: the sidecar's read stream is closed")
        try:
            i.fs.ping()
        except SmbError as e:
            _abort(context, e)
        return common_pb2.PingResponse()

    def Read(self, request_iterator, context) -> Iterator[pb.ReadResponse]:  # noqa: N802
        name = _instance(context)
        i = self.reg.inputs.get(name)
        if i is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, f"smb: input binding {name!r} is not initialised"
            )
        yield from ReadLoop(name, i, request_iterator, context, self.shutdown).run()


class ReadLoop:
    """Delivers one file at a time and waits for the sidecar's ack before the next."""

    def __init__(self, name: str, inp: _Input, requests, context, shutdown: threading.Event):
        self.name = name
        self.inp = inp
        self.requests = requests
        self.context = context
        self.shutdown = shutdown
        self.acks: queue.Queue[pb.ReadRequest | None] = queue.Queue()
        self.closed = threading.Event()
        self._labels = {"component": name, "target": inp.cfg.target}

    def _reader(self) -> None:
        try:
            for req in self.requests:
                self.acks.put(req)
        except Exception as e:  # noqa: BLE001
            log.info("%s: read stream ended: %s", self.name, e)
        finally:
            self.closed.set()
            self.acks.put(None)

    def _stopping(self) -> bool:
        return (
            self.closed.is_set()
            or self.shutdown.is_set()
            or self.inp.stop.is_set()
            or not self.context.is_active()
        )

    def run(self) -> Iterator[pb.ReadResponse]:
        threading.Thread(target=self._reader, name=f"acks-{self.name}", daemon=True).start()
        self.inp.stream_open = self.inp.stream_ever_opened = True
        metrics.INPUT_STREAM_UP.labels(**self._labels).set(1)
        log.info(
            "%s: read stream open, polling %r every %ss",
            self.name,
            self.inp.cfg.read.inbound_path,
            self.inp.cfg.read.poll_interval,
        )
        inbound = self.inp.inbound
        try:
            backlog: list[Claimed] = self._safe(inbound.recover) or []
            # Wait a poll interval before the next poll unless the last batch delivered
            # something. Without this, a claimed file that is still open (returned by every
            # poll, skipped by every load) would make the loop poll the share flat out.
            pause = False
            while not self._stopping():
                if not backlog:
                    if pause:
                        self._sleep(self.inp.cfg.read.poll_interval)
                        if self._stopping():
                            break
                    backlog = self._safe(inbound.poll) or []
                    pause = True
                    if not backlog:
                        continue
                c = backlog.pop(0)
                msg = self._safe(lambda c=c: inbound.load(c))
                if msg is None:
                    continue
                pause = False
                mid = uuid.uuid4().hex
                yield pb.ReadResponse(
                    data=msg.data, metadata=msg.metadata, content_type=msg.content_type, message_id=mid
                )
                ack = self._wait_ack(mid)
                if ack is None:
                    # Stream closed before the ack: leave the file claimed. It is
                    # re-delivered when a stream opens again (recover()).
                    log.warning(
                        "%s: stream closed before %r was acknowledged; it will be re-delivered",
                        self.name,
                        c.path,
                    )
                    break
                ok = not ack.HasField("response_error")
                self._safe(
                    lambda c=c, ok=ok, ack=ack: inbound.complete(c, ok=ok, error=ack.response_error.message)
                )
        finally:
            self.inp.stream_open = False
            metrics.INPUT_STREAM_UP.labels(**self._labels).set(0)
            log.info("%s: read stream closed", self.name)

    def _wait_ack(self, mid: str) -> pb.ReadRequest | None:
        while True:
            try:
                req = self.acks.get(timeout=1.0)
            except queue.Empty:
                if self.shutdown.is_set() or not self.context.is_active():
                    return None
                continue
            if req is None:
                return None
            if req.message_id == mid:
                return req
            log.debug("%s: ignoring ack for unknown message %s", self.name, req.message_id)

    def _sleep(self, seconds: float) -> None:
        # Wake early on shutdown or when the stream closes.
        deadline = seconds
        step = 0.25
        while deadline > 0 and not self._stopping():
            self.closed.wait(min(step, deadline))
            deadline -= step

    def _safe[T](self, fn: Callable[[], T]) -> T | None:
        """Errors in the poll loop are logged and retried next poll, never fatal to the stream:
        the sidecar does not reopen a stream that ends."""
        try:
            return fn()
        except SmbError as e:
            log.error("%s: %s", self.name, e)
        except Exception:
            log.exception("%s: unexpected error in the poll loop", self.name)
        self._sleep(self.inp.cfg.read.poll_interval)
        return None
