"""--exit-when-idle: exit once the sidecar's connection has been gone long enough."""

from __future__ import annotations

import threading

from dapr_smb_binding import idle

SOCK = "/tmp/dapr-components-sockets/smb.sock"
HEADER = "Num       RefCount Protocol Flags    Type St Inode Path\n"


def row(inode: int, state: str, path: str = "///tmp/dapr-components-sockets/smb.sock") -> str:
    return f"{inode:016x}: 00000002 00000000 00000000 0001 {state} {inode} {path}\n"


def test_counts_connections_to_our_socket_only(tmp_path):
    table = tmp_path / "unix"
    table.write_text(
        HEADER
        + row(1, "01")  # our listener
        + row(2, "03")  # daprd's end
        + row(3, "03")  # our accepted end
        + row(4, "03", "/run/other.sock")
        + "0000000000000005: 00000002 00000000 00000000 0001 03 5\n"  # no path
    )
    assert idle.sidecar_connections(SOCK, str(table)) == 2


def watch_fires(counts: list[int], timeout: float) -> bool:
    """Run watch() over per-second connection counts; True if it shut down before they ran out."""
    script = list(counts)
    now = [0.0]
    shutdown = threading.Event()
    ran_out = []

    def connections(_path: str) -> int:
        if not script:
            ran_out.append(True)
            shutdown.set()
            return 0
        now[0] += 1.0
        return script.pop(0)

    idle.watch(SOCK, timeout, shutdown, connections=connections, interval=0, clock=lambda: now[0])
    return not ran_out


def test_exits_after_the_sidecar_has_been_gone_long_enough():
    assert watch_fires([2, 2, 0, 0, 0, 0], timeout=3)


def test_waits_for_the_sidecar_to_connect_first():
    assert not watch_fires([0] * 10, timeout=3)


def test_a_reconnect_resets_the_clock():
    assert not watch_fires([2, 0, 0, 2, 0, 0], timeout=3)  # discovery, then init: a gap, not an exit


def test_a_short_gap_is_not_an_exit():
    assert not watch_fires([2, 0, 0, 2, 2, 2], timeout=3)
