"""Prometheus metrics and the /healthz endpoint, served when --metrics-port is set."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Callable

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, exposition

_LABELS = ("component", "target")

OPERATIONS = Counter(
    "smb_binding_operations",
    "Output binding operations by result.",
    (*_LABELS, "operation", "result"),
)
OPERATION_SECONDS = Histogram(
    "smb_binding_operation_seconds",
    "Output binding operation duration.",
    (*_LABELS, "operation"),
)
FILES_CLAIMED = Counter("smb_binding_files_claimed", "Inbound files claimed.", _LABELS)
FILES_PROCESSED = Counter("smb_binding_files_processed", "Inbound files acknowledged by the app.", _LABELS)
FILES_FAILED = Counter(
    "smb_binding_files_failed", "Inbound files the app rejected or that could not be read.", _LABELS
)
SHARING_VIOLATIONS = Counter(
    "smb_binding_sharing_violations",
    "Inbound files skipped because another client had them open.",
    _LABELS,
)
CLAIMS_RECOVERED = Counter(
    "smb_binding_claims_recovered",
    "Files returned to inboundPath from a replica whose heartbeat went stale.",
    _LABELS,
)
RECONNECTS = Counter("smb_binding_reconnects", "SMB sessions re-established after expiry or loss.", _LABELS)
AUTH_FAILURES = Counter("smb_binding_auth_failures", "Failed authentication attempts.", _LABELS)
STALE_TEMP_FILES_REMOVED = Counter(
    "smb_binding_stale_temp_files_removed", "Orphaned temp files deleted by staleTempFileAge.", _LABELS
)
INPUT_STREAM_UP = Gauge("smb_binding_input_stream_up", "1 while the sidecar's Read stream is open.", _LABELS)


ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - probes and scrapers reach the pod IP


def serve(
    port: int, healthy: Callable[[], tuple[bool, str]], host: str = ALL_INTERFACES
) -> http.server.HTTPServer:
    """Serve /metrics and /healthz on a background thread.

    /healthz exists because the sidecar does not reconnect a broken input
    stream, and does not surface a component's Ping in its own health check:
    a Kubernetes probe on this endpoint is what restarts the container.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/metrics"):
                body = exposition.generate_latest(REGISTRY)
                self._reply(200, body, exposition.CONTENT_TYPE_LATEST)
            elif self.path.startswith("/healthz"):
                ok, detail = healthy()
                self._reply(200 if ok else 503, (detail + "\n").encode(), "text/plain; charset=utf-8")
            else:
                self._reply(404, b"not found\n", "text/plain")

        def _reply(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, name="metrics", daemon=True).start()
    return srv
