"""Exit once the sidecar has gone away, so a Job's pod can complete.

A pod is only Succeeded once every container has stopped, and the Dapr injector
adds pluggable components to spec.containers. So when a Job's app finishes and
shuts daprd down, this process would keep running and the Job would never
complete. Nothing signals us: the kubelet only does that when the pod is torn
down, which is exactly what is not happening.

daprd counts as seen from its first Init: it calls that on our socket before
anything else, and it reaches us in-process, so it cannot be missed. Polling
alone can: daprd opens its HTTP port only once every component has loaded, and
a Job with nothing to do shuts it down well under a second later.

From then on daprd counts as running while either holds:
- its connection to our socket, which it opens at its first Init, during
  startup, and which goes when it exits. It is not enough on its own: daprd
  closes it after 30 minutes without calls while it keeps running.
- its HTTP port on localhost, which every container in a pod shares and which
  daprd listens on from the end of its startup for as long as it runs.

The Component sets the default with --exit-when-idle, and a pod overrides it
with SMB_BINDING_EXIT_WHEN_IDLE, which the container maps from a pod annotation
with the downward API. The injected container is the same in every pod that
uses the Component, so the annotation is what tells a Job's pod from a
Deployment's. A Deployment's pod sets it to "off": there a sidecar restart is
routine and the component has to still be listening afterwards.

On by default on the Component, because forgetting the annotation should be
loud: a Deployment that exits restarts, fails and alerts, while a Job whose
binding never exits just hangs and its CronJob never runs again.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping

from .config import parse_duration

log = logging.getLogger(__name__)

# The pod's override of --exit-when-idle, e.g. "30s", or OFF. Unset or empty: the flag.
TIMEOUT_ENV = "SMB_BINDING_EXIT_WHEN_IDLE"
OFF = "off"

# daprd's HTTP port, as the injector configures it; DAPR_HTTP_PORT if set.
DEFAULT_DAPR_HTTP_PORT = 3500


def timeout(default: float, environ: Mapping[str, str] = os.environ) -> float:
    """The idle timeout in seconds, or 0 when off.

    The pod's SMB_BINDING_EXIT_WHEN_IDLE wins when set, and "off" turns idle exit
    off. Unset or empty, which is what a pod without the annotation gets, falls
    back to default, the Component's --exit-when-idle.

    Raises ConfigError for anything else that is not a duration, so a typo in
    the annotation stops the binding instead of being silently ignored.
    """
    value = environ.get(TIMEOUT_ENV, "")
    if value.strip().lower() == OFF:
        return 0.0
    return parse_duration(TIMEOUT_ENV, value, default)


# Linux's table of unix sockets: a connection accepted on our socket is listed
# under its path, in state 03 (connected), until we close it.
PROC_NET_UNIX = "/proc/net/unix"
CONNECTED = "03"


def sidecar_running(socket_path: str, port: int | None = None) -> bool:
    return sidecar_connected(socket_path) or http_port_open(port)


def sidecar_connected(socket_path: str, table: str = PROC_NET_UNIX) -> bool:
    try:
        with open(table) as f:
            next(f)  # header
            for line in f:
                fields = line.split()
                if len(fields) == 8 and fields[7] == socket_path and fields[5] == CONNECTED:
                    return True
    except OSError:  # not Linux: the HTTP port alone
        pass
    return False


def http_port_open(port: int | None = None) -> bool:
    port = port or int(os.environ.get("DAPR_HTTP_PORT") or DEFAULT_DAPR_HTTP_PORT)
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def watch(
    timeout: float,
    shutdown: threading.Event,
    started: threading.Event,
    running: Callable[[], bool],
    *,
    interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Set shutdown once the sidecar has been up and then gone for timeout seconds.

    started is set on the sidecar's first Init. Nothing happens until then or
    until running() has been seen true, so a slow-starting daprd does not find
    us already gone.
    """
    seen = False
    gone_since: float | None = None
    while not shutdown.wait(interval):
        if running():
            seen, gone_since = True, None
        elif seen or started.is_set():
            if gone_since is None:
                gone_since = clock()
            elif clock() - gone_since >= timeout:
                log.info("the sidecar has been gone for %.0fs; exiting so the pod can complete", timeout)
                shutdown.set()
