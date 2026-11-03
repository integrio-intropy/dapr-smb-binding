"""Restart a sidecar that loaded this binding before we were running.

daprd connects to a pluggable component's socket once, while it loads its
components, and never reconnects. When this container restarts on its own (an
OOM kill, a failed liveness probe) and daprd keeps running, daprd still holds
the old, dead connection: every call fails with "error reading from server:
EOF" until daprd itself restarts. In a Deployment the pod looks healthy, so
nothing would ever restart it.

So at startup we look for exactly that state: daprd is up (its HTTP port
answers, which it opens only once all its components have loaded), its
metadata lists a component of our type, and it has not called our Init. Then
it loaded us before we were here, and we ask it to shut down. Kubernetes
restarts it in the same pod (restartPolicy Always or, after an error,
OnFailure), and the new daprd connects to us.

The metadata check keeps us from shutting down a daprd that never meant to
load us, e.g. one whose app is not in the Component's scopes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)


def watch(
    component_type: str,
    started: threading.Event,
    shutdown: threading.Event,
    port: int,
    *,
    grace: float = 10.0,
    give_up: float = 120.0,
    interval: float = 2.0,
    metadata: Callable[[int], dict | None] | None = None,
    stop_sidecar: Callable[[int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Ask daprd to shut down if it loaded component_type before we started.

    Waits grace seconds first, so an Init that is on its way arrives. Gives up
    after give_up seconds: by then daprd either connected to us or is not
    coming. Returns whether it asked daprd to shut down.
    """
    metadata = metadata or _metadata
    stop_sidecar = stop_sidecar or _shutdown
    start = clock()
    while not shutdown.wait(interval):
        if started.is_set():
            return False
        elapsed = clock() - start
        if elapsed >= give_up:
            return False
        if elapsed < grace:
            continue
        md = metadata(port)
        if md is None:
            continue  # daprd is not up (yet)
        if not any(c.get("type") == component_type for c in md.get("components", [])):
            return False  # daprd runs without us on purpose
        log.warning(
            "daprd loaded %s before this binding was running and holds a dead connection to it; "
            "asking it to shut down so it restarts and reconnects",
            component_type,
        )
        try:
            stop_sidecar(port)
        except OSError as e:
            log.error("could not shut daprd down: %s", e)
        return True
    return False


def _metadata(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1.0/metadata", timeout=2) as r:  # noqa: S310
            return json.load(r)
    except (OSError, ValueError):
        return None


def _shutdown(port: int) -> None:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1.0/shutdown", method="POST")
    urllib.request.urlopen(req, timeout=5).close()  # noqa: S310
