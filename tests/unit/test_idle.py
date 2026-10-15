"""--exit-when-idle: exit once the sidecar has been gone long enough."""

from __future__ import annotations

import socket
import threading

from dapr_smb_binding import idle


def test_sidecar_running_follows_the_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    assert idle.sidecar_running(port)
    srv.close()
    assert not idle.sidecar_running(port)


def watch_fires(states: list[bool], timeout: float) -> bool:
    """Run watch() over per-second sidecar states; True if it shut down before they ran out."""
    script = list(states)
    now = [0.0]
    shutdown = threading.Event()
    ran_out = []

    def running() -> bool:
        if not script:
            ran_out.append(True)
            shutdown.set()
            return True
        now[0] += 1.0
        return script.pop(0)

    idle.watch(timeout, shutdown, running=running, interval=0, clock=lambda: now[0])
    return not ran_out


UP, GONE = True, False


def test_exits_after_the_sidecar_has_been_gone_long_enough():
    assert watch_fires([UP, UP, GONE, GONE, GONE, GONE], timeout=3)


def test_waits_for_the_sidecar_to_start_first():
    assert not watch_fires([GONE] * 10, timeout=3)


def test_a_restart_within_the_timeout_resets_the_clock():
    assert not watch_fires([UP, GONE, GONE, UP, GONE, GONE], timeout=3)
