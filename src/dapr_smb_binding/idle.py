"""Exit once the sidecar has gone away, so a Job's pod can complete.

A pod is only Succeeded once every container has stopped, and the Dapr injector
adds pluggable components to spec.containers. So when a Job's app finishes and
shuts daprd down, this process would keep running and the Job would never
complete. Nothing signals us: the kubelet only does that when the pod is torn
down, which is exactly what is not happening.

The signal is daprd's connection to our socket, as in dapr-ftp-binding's
-exit-when-idle. Python's gRPC server does not report connections, but Linux
lists them in /proc/net/unix, and in a pod every container shares the network
namespace, so the sidecar's connection is visible from here.

Off by default, and it must stay off for Deployments: there a sidecar restart
is routine and the component has to still be listening afterwards.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

PROC_NET_UNIX = "/proc/net/unix"
CONNECTED = "03"


def sidecar_connections(socket_path: str, table: str = PROC_NET_UNIX) -> int:
    """How many connected sockets are bound to our socket's path."""
    want = os.path.normpath(socket_path)
    with open(table) as f:
        next(f)  # header
        return sum(
            1
            for cols in (line.split() for line in f)
            if len(cols) > 7 and cols[5] == CONNECTED and os.path.normpath(cols[7]) == want
        )


def watch(
    socket_path: str,
    timeout: float,
    shutdown: threading.Event,
    *,
    connections: Callable[[str], int] = sidecar_connections,
    interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Set shutdown once the sidecar has connected and then stayed away for timeout seconds.

    Nothing happens until the sidecar has connected once, so a slow-starting daprd
    does not find us already gone.
    """
    seen = False
    idle_since: float | None = None
    while not shutdown.wait(interval):
        if connections(socket_path):
            seen, idle_since = True, None
        elif seen:
            idle_since = idle_since or clock()
            if clock() - idle_since >= timeout:
                log.info("the sidecar has been gone for %.0fs; exiting so the pod can complete", timeout)
                shutdown.set()
