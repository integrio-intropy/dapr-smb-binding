"""The production image as a pluggable component under a real daprd. Run with `make e2e`.

This test is also daprd's app: daprd delivers input binding events to
POST /smb on this container.
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

import pytest

from dapr_smb_binding.config import parse
from dapr_smb_binding.errors import SmbError
from dapr_smb_binding.smb import SmbFileSystem

pytestmark = pytest.mark.integration

DAPR = os.environ.get("SMB_IT_DAPR_URL", "")
APP_PORT = 8080


@pytest.fixture(scope="module")
def app():
    """Answers daprd: 200 for the binding's OPTIONS probe, records each POST."""
    received: queue.Queue = queue.Queue()
    reject = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_OPTIONS(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.put((self.path, dict(self.headers), body))
            self.send_response(500 if reject.is_set() else 200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", APP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield received, reject
    srv.shutdown()


def dapr(op: str, data: str = "", **md: str):
    req = urllib.request.Request(
        f"{DAPR}/v1.0/bindings/smb",
        data=json.dumps({"operation": op, "data": data, "metadata": md}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
        return r.status, r.read()


def wait_ready(app):
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{DAPR}/v1.0/healthz", timeout=2) as r:  # noqa: S310
                if r.status == 204:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    pytest.fail("daprd never became healthy")


def names_in(fs, folder: str) -> list[str]:
    try:
        return sorted(e.name for e in fs.listdir(folder) if not e.is_dir)
    except SmbError:
        return []


def eventually(fn, want, timeout: float = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn() == want:
            return
        time.sleep(0.5)
    assert fn() == want


@pytest.fixture(scope="module")
def share():
    cfg, _ = parse(
        {
            "address": os.environ["SMB_IT_ADDRESS"],
            "share": os.environ["SMB_IT_SHARE"],
            "rootPath": "e2e",
            "username": os.environ["SMB_IT_PARTNER_USER"],
            "password": os.environ["SMB_IT_PARTNER_PASSWORD"],
            "kdcs": os.environ["SMB_IT_KDC"],
        }
    )
    fs = SmbFileSystem(cfg, component="e2e-partner")
    fs.makedirs("e2e/in")
    yield fs
    fs.close()


@pytest.mark.skipif(not DAPR, reason="run through `make e2e`")
def test_output_through_daprd(app, share):
    wait_ready(app)
    # Dapr's HTTP API JSON-encodes a string `data` before it reaches any binding
    # (SFTP and FTP too); SDKs over gRPC pass raw bytes. So the file holds JSON.
    status, body = dapr("create", "<hello/>", fileName="hello.xml")
    assert status == 200
    assert json.loads(body) == {"fileName": "hello.xml"}
    assert json.loads(share.read("e2e/out/hello.xml")) == "<hello/>"

    status, body = dapr("get", fileName="hello.xml")
    assert json.loads(body) == "<hello/>"

    # Never overwrites: daprd surfaces the component's error.
    with pytest.raises(urllib.error.HTTPError) as e:
        dapr("create", "<again/>", fileName="hello.xml")
    assert "already exists" in e.value.read().decode() or e.value.code == 500

    status, _ = dapr("rename", fileName="hello.xml", newFileName="renamed.xml")
    assert status == 200
    names = [x["fileName"] for x in json.loads(dapr("list")[1])]
    assert "renamed.xml" in names and "hello.xml" not in names
    dapr("delete", fileName="renamed.xml")


@pytest.mark.skipif(not DAPR, reason="run through `make e2e`")
def test_input_through_daprd(app, share):
    received, reject = app
    wait_ready(app)
    share.write("e2e/in/order.xml", b"<order/>", exclusive_create=True)

    path, headers, body = received.get(timeout=60)
    assert path == "/smb"
    assert body == b"<order/>"
    assert headers.get("fileName") == "order.xml" or headers.get("Filename") == "order.xml"

    eventually(lambda: names_in(share, "e2e/in/processed"), ["order.xml"])

    # The app rejects: the file goes to error/.
    reject.set()
    share.write("e2e/in/bad.xml", b"<bad/>", exclusive_create=True)
    received.get(timeout=60)
    eventually(lambda: names_in(share, "e2e/in/error"), ["bad.xml"])
    assert names_in(share, "e2e/in") == []
