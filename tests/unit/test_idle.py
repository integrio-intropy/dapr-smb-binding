"""SMB_BINDING_EXIT_WHEN_IDLE: exit once the sidecar has been gone long enough."""

from __future__ import annotations

import socket
import threading

import pytest

from dapr_smb_binding import __main__ as entrypoint
from dapr_smb_binding import idle
from dapr_smb_binding.config import ConfigError


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, 30.0),  # the variable is not mapped: the Component's default
        ({idle.TIMEOUT_ENV: ""}, 30.0),  # mapped from an annotation the pod does not set
        ({idle.TIMEOUT_ENV: "  "}, 30.0),
        ({idle.TIMEOUT_ENV: "1m30s"}, 90.0),  # the pod overrides the default
        ({idle.TIMEOUT_ENV: "off"}, 0.0),  # a Deployment's pod
        ({idle.TIMEOUT_ENV: " OFF "}, 0.0),
    ],
)
def test_timeout_the_pod_overrides_the_component(environ: dict[str, str], expected: float):
    assert idle.timeout(30.0, environ) == expected


def test_timeout_is_off_when_neither_sets_it():
    assert idle.timeout(0.0, {}) == 0.0


@pytest.mark.parametrize("value", ["30", "soon", "0s", "false"])
def test_timeout_rejects_what_is_not_a_positive_duration_or_off(value: str):
    with pytest.raises(ConfigError, match=idle.TIMEOUT_ENV):
        idle.timeout(30.0, {idle.TIMEOUT_ENV: value})


def test_main_refuses_to_start_on_an_invalid_timeout(monkeypatch, tmp_path):
    # Before it listens: a typo in the annotation must stop the binding, not be ignored.
    monkeypatch.setenv(idle.TIMEOUT_ENV, "30")
    monkeypatch.setenv(entrypoint.SOCKET_FOLDER_ENV, str(tmp_path))
    assert entrypoint.main(["--exit-when-idle", "30s"]) == 2
    assert not (tmp_path / "smb.sock").exists()


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
