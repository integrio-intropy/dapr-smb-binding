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


def test_http_port_open_follows_the_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    assert idle.http_port_open(port)
    srv.close()
    assert not idle.http_port_open(port)


SOCK = "/tmp/dapr-components-sockets/smb.sock"
HEADER = "Num       RefCount Protocol Flags    Type St Inode Path\n"
LISTENING = f"0000000000000000: 00000002 00000000 00010000 0001 01 3727884 {SOCK}\n"
DAPRD = f"0000000000000000: 00000003 00000000 00000000 0001 03 3727887 {SOCK}\n"
OTHER = "0000000000000000: 00000003 00000000 00000000 0001 03 3727890 /run/other.sock\n"
UNNAMED = "0000000000000000: 00000003 00000000 00000000 0001 03 3727891\n"


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([LISTENING, DAPRD, UNNAMED], True),
        ([LISTENING, OTHER, UNNAMED], False),  # nothing connected to our socket
        ([], False),
    ],
)
def test_sidecar_connected_reads_the_unix_socket_table(tmp_path, rows: list[str], expected: bool):
    table = tmp_path / "unix"
    table.write_text(HEADER + "".join(rows))
    assert idle.sidecar_connected(SOCK, str(table)) is expected


def test_sidecar_connected_is_false_without_the_table(tmp_path):
    assert not idle.sidecar_connected(SOCK, str(tmp_path / "missing"))


def watch_fires(
    states: list[bool], timeout: float, *, init_at: int | None = None, start_wait: float = 0
) -> bool:
    """Run watch() over per-second sidecar states; True if it shut down before they ran out.

    init_at: the second the sidecar's first Init arrives, if it does.
    """
    script = list(states)
    now = [0.0]
    shutdown, started = threading.Event(), threading.Event()
    ran_out = []

    def running() -> bool:
        if not script:
            ran_out.append(True)
            shutdown.set()
            return True
        now[0] += 1.0
        if init_at is not None and now[0] >= init_at:
            started.set()
        return script.pop(0)

    idle.watch(timeout, shutdown, started, running, interval=0, start_wait=start_wait, clock=lambda: now[0])
    return not ran_out


UP, GONE = True, False


def test_exits_after_the_sidecar_has_been_gone_long_enough():
    assert watch_fires([UP, UP, GONE, GONE, GONE, GONE], timeout=3)


def test_waits_for_the_sidecar_to_start_first():
    assert not watch_fires([GONE] * 10, timeout=3)


def test_a_restart_within_the_timeout_resets_the_clock():
    assert not watch_fires([UP, GONE, GONE, UP, GONE, GONE], timeout=3)


def test_an_init_counts_as_seen_when_polling_missed_the_sidecar():
    # A Job with nothing to do: daprd called Init, then came and went between two polls.
    assert watch_fires([GONE] * 5, timeout=3, init_at=1)


def test_waits_out_a_slow_start_after_init_while_the_sidecar_runs():
    assert not watch_fires([GONE, UP, UP, UP, UP, UP], timeout=3, init_at=1)


def test_exits_when_the_sidecar_never_connects_within_the_start_wait():
    # daprd looked for our socket before we listened, and died before reaching us.
    assert watch_fires([GONE] * 10, timeout=3, start_wait=5)


def test_a_sidecar_seen_within_the_start_wait_is_left_to_the_idle_timer():
    assert not watch_fires([GONE, GONE, UP, UP, UP, UP, UP, UP, UP, UP], timeout=3, start_wait=5)


def test_an_init_within_the_start_wait_is_left_to_the_idle_timer():
    # Seen through Init only: the idle timeout, not the start wait, decides.
    assert not watch_fires([GONE, GONE], timeout=3, init_at=1, start_wait=1)
