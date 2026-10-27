"""End to end over a real gRPC Unix socket, the way the Dapr sidecar talks to us."""

from __future__ import annotations

import json
import pathlib
import queue
import shutil
import tempfile
import threading
from concurrent import futures

import grpc
import pytest
from grpc_reflection.v1alpha import reflection, reflection_pb2, reflection_pb2_grpc
from localfs import LocalFS

from dapr_smb_binding._proto import bindings_pb2 as pb
from dapr_smb_binding._proto import bindings_pb2_grpc as pb_grpc
from dapr_smb_binding._proto import common_pb2
from dapr_smb_binding.server import InputService, OutputService, Registry

PROPS = {
    "address": "fs01.corp.example.com",
    "share": "exchange",
    "rootPath": "out",
    "inboundPath": "in",
    "username": "svc@CORP.EXAMPLE.COM",
    "password": "pw",
    "pollInterval": "100ms",
}
MD = (("x-component-instance", "partner-share"),)


@pytest.fixture
def env(tmp_path):
    lfs = LocalFS(tmp_path)
    reg = Registry(lambda cfg, name: lfs, max_message_size=1 << 20)
    shutdown = threading.Event()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_OutputBindingServicer_to_server(OutputService(reg), server)
    pb_grpc.add_InputBindingServicer_to_server(InputService(reg, shutdown), server)
    names = (
        "dapr.proto.components.v1.OutputBinding",
        "dapr.proto.components.v1.InputBinding",
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(names, server)
    # Unix socket paths are limited to ~104 bytes; pytest's tmp_path is too long on macOS.
    sockdir = tempfile.mkdtemp(prefix="smb", dir="/tmp")  # noqa: S108
    server.add_insecure_port(f"unix://{pathlib.Path(sockdir) / 'smb.sock'}")
    server.start()
    channel = grpc.insecure_channel(f"unix://{pathlib.Path(sockdir) / 'smb.sock'}")
    yield lfs, channel, reg
    shutdown.set()
    channel.close()
    server.stop(1).wait()
    shutil.rmtree(sockdir, ignore_errors=True)


def init(ch, kind="output", props=PROPS):
    if kind == "output":
        req = pb.OutputBindingInitRequest(metadata=common_pb2.MetadataRequest(properties=props))
        return pb_grpc.OutputBindingStub(ch).Init(req, metadata=MD)
    req = pb.InputBindingInitRequest(metadata=common_pb2.MetadataRequest(properties=props))
    return pb_grpc.InputBindingStub(ch).Init(req, metadata=MD)


def eventually(fn, want, timeout=5.0):
    done = threading.Event()
    for _ in range(int(timeout / 0.05)):
        if fn() == want:
            return
        done.wait(0.05)
    assert fn() == want


def test_reflection_lists_both_services(env):
    _, ch, _ = env
    stub = reflection_pb2_grpc.ServerReflectionStub(ch)
    resp = next(stub.ServerReflectionInfo(iter([reflection_pb2.ServerReflectionRequest(list_services="")])))
    names = {s.name for s in resp.list_services_response.service}
    assert {"dapr.proto.components.v1.OutputBinding", "dapr.proto.components.v1.InputBinding"} <= names


def test_output(env):
    lfs, ch, _ = env
    init(ch)
    out = pb_grpc.OutputBindingStub(ch)
    assert list(out.ListOperations(pb.ListOperationsRequest(), metadata=MD).operations) == [
        "create", "get", "list", "delete", "rename",
    ]  # fmt: skip
    r = out.Invoke(
        pb.InvokeRequest(operation="create", data=b"<a/>", metadata={"fileName": "a.xml"}), metadata=MD
    )
    assert json.loads(r.data) == {"fileName": "a.xml"}
    with pytest.raises(grpc.RpcError) as e:
        out.Invoke(
            pb.InvokeRequest(operation="create", data=b"x", metadata={"fileName": "a.xml"}), metadata=MD
        )
    assert e.value.code() is grpc.StatusCode.ALREADY_EXISTS


def test_bad_config_fails_init(env):
    _, ch, _ = env
    with pytest.raises(grpc.RpcError) as e:
        init(ch, props={**PROPS, "address": "1.2.3.4"})
    assert e.value.code() is grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize("props", [PROPS, {**PROPS, "address": "1.2.3.4"}])
def test_any_init_marks_the_sidecar_started(env, props):
    # Failed or not, an Init proves daprd is running: idle exit counts it as seen.
    _, ch, reg = env
    assert not reg.started.is_set()
    try:
        init(ch, kind="input", props=props)
    except grpc.RpcError:
        pass
    assert reg.started.is_set()


def read_stream(ch):
    acks: queue.Queue = queue.Queue()
    requests = iter(acks.get, None)
    return pb_grpc.InputBindingStub(ch).Read(requests, metadata=MD), acks


def test_input_ack_and_nack(env):
    lfs, ch, reg = env
    lfs.put("in/a.xml", b"<a/>", age=2)
    lfs.put("in/b.xml", b"<b/>", age=1)
    init(ch, "input")
    responses, acks = read_stream(ch)
    a = next(responses)
    assert (a.metadata["fileName"], a.data, a.content_type) == ("a.xml", b"<a/>", "application/xml")
    acks.put(pb.ReadRequest(message_id=a.message_id))
    b = next(responses)
    acks.put(pb.ReadRequest(message_id=b.message_id, response_error=pb.AckResponseError(message="bad")))
    eventually(lambda: lfs.files("in"), ["error/b.xml", "processed/a.xml"])
    assert reg.health() == (True, "ok")
    acks.put(None)
    responses.cancel()


def test_unanswered_file_is_delivered_again(env):
    lfs, ch, reg = env
    lfs.put("in/a.xml")
    init(ch, "input")
    responses, acks = read_stream(ch)
    assert next(responses).metadata["fileName"] == "a.xml"
    acks.put(None)  # the sidecar goes away without answering
    responses.cancel()
    eventually(lambda: reg.health()[0], False)
    assert lfs.files("in") == ["a.xml"]  # still where it was

    responses, acks = read_stream(ch)
    again = next(responses)
    assert again.metadata["fileName"] == "a.xml"
    acks.put(pb.ReadRequest(message_id=again.message_id))
    eventually(lambda: lfs.files("in"), ["processed/a.xml"])
    acks.put(None)
    responses.cancel()


def test_file_held_open_does_not_make_the_loop_spin(env):
    lfs, ch, _ = env
    lfs.put("in/a.xml")
    lfs.open_by_partner.add("in/a.xml")
    init(ch, "input")
    responses, acks = read_stream(ch)
    got: list = []
    t = threading.Thread(target=lambda: got.append(next(responses)), daemon=True)
    t.start()
    lfs.calls.clear()
    threading.Event().wait(1.0)
    polls = sum(1 for op, path in lfs.calls if op == "list" and path == "in")
    assert polls <= 15, polls  # pollInterval 100ms: about 10, not thousands

    lfs.open_by_partner.clear()
    t.join(5)
    acks.put(pb.ReadRequest(message_id=got[0].message_id))
    acks.put(None)
    responses.cancel()
