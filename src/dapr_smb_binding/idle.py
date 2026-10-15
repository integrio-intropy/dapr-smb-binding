"""Exit once the sidecar has gone away, so a Job's pod can complete.

A pod is only Succeeded once every container has stopped, and the Dapr injector
adds pluggable components to spec.containers. So when a Job's app finishes and
shuts daprd down, this process would keep running and the Job would never
complete. Nothing signals us: the kubelet only does that when the pod is torn
down, which is exactly what is not happening.

The signal is daprd's HTTP port on localhost, which every container in a pod
shares and which daprd listens on for as long as it runs. Its connection to
our socket is not a usable signal: daprd closes that after 30 minutes without
calls while it keeps running.

Off by default, and it must stay off for Deployments: there a sidecar restart
is routine and the component has to still be listening afterwards.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# daprd's HTTP port, as the injector configures it; DAPR_HTTP_PORT if set.
DEFAULT_DAPR_HTTP_PORT = 3500


def sidecar_running(port: int | None = None) -> bool:
    port = port or int(os.environ.get("DAPR_HTTP_PORT") or DEFAULT_DAPR_HTTP_PORT)
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def watch(
    timeout: float,
    shutdown: threading.Event,
    *,
    running: Callable[[], bool] = sidecar_running,
    interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Set shutdown once the sidecar has been up and then gone for timeout seconds.

    Nothing happens until the sidecar has been seen once, so a slow-starting
    daprd does not find us already gone.
    """
    seen = False
    gone_since: float | None = None
    while not shutdown.wait(interval):
        if running():
            seen, gone_since = True, None
        elif seen:
            if gone_since is None:
                gone_since = clock()
            elif clock() - gone_since >= timeout:
                log.info("the sidecar has been gone for %.0fs; exiting so the pod can complete", timeout)
                shutdown.set()
