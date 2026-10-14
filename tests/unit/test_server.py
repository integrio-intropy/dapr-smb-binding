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


@pytest.fixture
def env(tmp_path: pathlib.Path):
    lfs = LocalFS(tmp_path / "share")
    lfs.root.mkdir()
    shutdown = threading.Event()
    reg = Registry(lambda cfg, name: lfs, instance_id="pod-a", max_message_size=1 << 20)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_OutputBindingServicer_to_server(OutputService(reg), server)
    pb_grpc.add_InputBindingServicer_to_server(InputService(reg, shutdown), server)
    reflection.enable_server_reflection(
        (
            "dapr.proto.components.v1.OutputBinding",
            "dapr.proto.components.v1.InputBinding",
            reflection.SERVICE_NAME,
        ),
        server,
    )
    # Unix socket paths are limited to ~104 bytes; pytest's tmp_path is too long on macOS.
    sockdir = tempfile.mkdtemp(prefix="smb", dir="/tmp")  # noqa: S108
    sock = pathlib.Path(sockdir) / "smb.sock"
    server.add_insecure_port(f"unix://{sock}")
    server.start()
    channel = grpc.insecure_channel(f"unix://{sock}")
    yield lfs, channel, reg
    shutdown.set()
    channel.close()
    server.stop(1).wait()
    shutil.rmtree(sockdir, ignore_errors=True)


MD = (("x-component-instance", "astro-share"),)


def test_reflection_lists_both_binding_services(env):
    _, ch, _ = env
    stub = reflection_pb2_grpc.ServerReflectionStub(ch)
    resp = next(stub.ServerReflectionInfo(iter([reflection_pb2.ServerReflectionRequest(list_services="")])))
    names = {s.name for s in resp.list_services_response.service}
    assert {"dapr.proto.components.v1.OutputBinding", "dapr.proto.components.v1.InputBinding"} <= names


def test_output_init_list_operations_invoke(env):
    lfs, ch, _ = env
    out = pb_grpc.OutputBindingStub(ch)
    out.Init(pb.OutputBindingInitRequest(metadata=common_pb2.MetadataRequest(properties=PROPS)), metadata=MD)
    ops = out.ListOperations(pb.ListOperationsRequest(), metadata=MD).operations
    assert list(ops) == ["create", "get", "list", "delete", "rename"]

    r = out.Invoke(
        pb.InvokeRequest(operation="create", data=b"<a/>", metadata={"fileName": "a.xml"}), metadata=MD
    )
    assert json.loads(r.data) == {"fileName": "a.xml"}
    assert lfs.files() == ["out/a.xml"]
    out.Ping(common_pb2.PingRequest(), metadata=MD)


def test_output_errors_map_to_grpc_status(env):
    lfs, ch, _ = env
    out = pb_grpc.OutputBindingStub(ch)
    out.Init(pb.OutputBindingInitRequest(metadata=common_pb2.MetadataRequest(properties=PROPS)), metadata=MD)
    lfs.put("out/a.xml")
    with pytest.raises(grpc.RpcError) as e:
        out.Invoke(
            pb.InvokeRequest(operation="create", data=b"x", metadata={"fileName": "a.xml"}), metadata=MD
        )
    assert e.value.code() is grpc.StatusCode.ALREADY_EXISTS
    assert 'smb: rename "out/a.xml.tmp"' in e.value.details() or "out/a.xml" in e.value.details()


def test_bad_config_fails_init(env):
    _, ch, _ = env
    out = pb_grpc.OutputBindingStub(ch)
    with pytest.raises(grpc.RpcError) as e:
        out.Init(
            pb.OutputBindingInitRequest(
                metadata=common_pb2.MetadataRequest(properties={**PROPS, "address": "1.2.3.4"})
            ),
            metadata=MD,
        )
    assert e.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert "IP address" in e.value.details()


def test_uninitialised_instance(env):
    _, ch, _ = env
    with pytest.raises(grpc.RpcError) as e:
        pb_grpc.OutputBindingStub(ch).Invoke(
            pb.InvokeRequest(operation="list"), metadata=(("x-component-instance", "nope"),)
        )
    assert e.value.code() is grpc.StatusCode.FAILED_PRECONDITION


def _read_stream(ch):
    """Open Read like the sidecar does; returns (responses iterator, ack queue)."""
    acks: queue.Queue = queue.Queue()

    def requests():
        while True:
            item = acks.get()
            if item is None:
                return
            yield item

    stub = pb_grpc.InputBindingStub(ch)
    return stub.Read(requests(), metadata=MD), acks


def test_input_read_ack_and_nack(env):
    lfs, ch, reg = env
    lfs.put("in/a.xml", b"<a/>")
    lfs.put("in/b.xml", b"<b/>")
    inp = pb_grpc.InputBindingStub(ch)
    inp.Init(pb.InputBindingInitRequest(metadata=common_pb2.MetadataRequest(properties=PROPS)), metadata=MD)

    responses, acks = _read_stream(ch)
    first = next(responses)
    assert first.metadata["fileName"] == "a.xml"
    assert first.data == b"<a/>"
    assert first.content_type == "application/xml"
    acks.put(pb.ReadRequest(message_id=first.message_id))

    second = next(responses)
    assert second.metadata["fileName"] == "b.xml"
    acks.put(
        pb.ReadRequest(message_id=second.message_id, response_error=pb.AckResponseError(message="bad xml"))
    )

    # Wait for the nack to be applied, then close the stream.
    for _ in range(50):
        if lfs.files("in") == ["error/b.xml", "processed/a.xml"]:
            break
        threading.Event().wait(0.05)
    assert lfs.files("in") == ["error/b.xml", "processed/a.xml"]
    assert reg.health() == (True, "ok")
    acks.put(None)
    responses.cancel()


def test_unacked_file_is_redelivered_on_new_stream(env):
    lfs, ch, reg = env
    lfs.put("in/a.xml", b"<a/>")
    inp = pb_grpc.InputBindingStub(ch)
    inp.Init(pb.InputBindingInitRequest(metadata=common_pb2.MetadataRequest(properties=PROPS)), metadata=MD)

    responses, acks = _read_stream(ch)
    assert next(responses).metadata["fileName"] == "a.xml"
    acks.put(None)  # sidecar goes away without acking
    responses.cancel()
    for _ in range(50):
        if not reg.inputs["astro-share"].stream_open:
            break
        threading.Event().wait(0.05)
    ok, detail = reg.health()
    assert not ok and "astro-share" in detail
    assert lfs.files("in") == ["processing/pod-a/a.xml"]

    responses, acks = _read_stream(ch)
    again = next(responses)
    assert again.metadata["fileName"] == "a.xml"
    acks.put(pb.ReadRequest(message_id=again.message_id))
    for _ in range(50):
        if lfs.files("in") == ["processed/a.xml"]:
            break
        threading.Event().wait(0.05)
    assert lfs.files("in") == ["processed/a.xml"]
    acks.put(None)
    responses.cancel()


def test_open_claimed_file_does_not_make_the_loop_spin(env):
    lfs, ch, _ = env
    lfs.put("in/a.xml")
    # Claim (rename) works, the exclusive read does not: the partner still writes it.
    lfs.open_by_partner.add("in/processing/pod-a/a.xml")
    inp = pb_grpc.InputBindingStub(ch)
    inp.Init(pb.InputBindingInitRequest(metadata=common_pb2.MetadataRequest(properties=PROPS)), metadata=MD)

    responses, acks = _read_stream(ch)
    got: list = []
    t = threading.Thread(target=lambda: got.append(next(responses)), daemon=True)
    t.start()
    lfs.calls.clear()
    threading.Event().wait(1.0)
    polls = sum(1 for op, path in lfs.calls if op == "list" and path == "in")
    # pollInterval is 100ms: about 10 polls a second, not thousands.
    assert polls <= 15, polls

    lfs.open_by_partner.clear()
    t.join(5)
    assert got and got[0].metadata["fileName"] == "a.xml"
    acks.put(pb.ReadRequest(message_id=got[0].message_id))
    acks.put(None)
    responses.cancel()
