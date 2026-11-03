import threading

from dapr_smb_binding import rejoin

OURS = {"components": [{"name": "partner-share", "type": "bindings.smb"}]}
OTHER = {"components": [{"name": "kubernetes", "type": "secretstores.kubernetes"}]}


class Clock:
    """Advances one interval per wait, so watch() runs without sleeping."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Shutdown(threading.Event):
    def __init__(self, clock, step):
        super().__init__()
        self.clock, self.step = clock, step

    def wait(self, timeout=None):
        self.clock.now += self.step
        return self.is_set()


def run(metadata, started=None, grace=10.0, give_up=120.0):
    clock = Clock()
    stopped = []
    asked = rejoin.watch(
        "bindings.smb",
        started or threading.Event(),
        Shutdown(clock, 2.0),
        3500,
        grace=grace,
        give_up=give_up,
        metadata=metadata,
        stop_sidecar=stopped.append,
        clock=clock,
    )
    return asked, stopped, clock.now


def test_shuts_down_a_sidecar_that_loaded_us_before_we_ran():
    asked, stopped, at = run(lambda _port: OURS)
    assert asked and stopped == [3500]
    assert at >= 10.0  # not before the grace period


def test_leaves_a_sidecar_that_called_init_alone():
    started = threading.Event()
    started.set()
    asked, stopped, _ = run(lambda _port: OURS, started=started)
    assert not asked and stopped == []


def test_waits_for_a_sidecar_that_is_not_up_yet():
    calls = []

    def metadata(port):
        calls.append(port)
        return OURS if len(calls) >= 3 else None

    asked, stopped, _ = run(metadata)
    assert asked and stopped == [3500] and len(calls) == 3


def test_leaves_a_sidecar_without_our_component_alone():
    asked, stopped, _ = run(lambda _port: OTHER)
    assert not asked and stopped == []


def test_gives_up_when_no_sidecar_comes():
    asked, stopped, at = run(lambda _port: None, give_up=30.0)
    assert not asked and stopped == []
    assert 30.0 <= at < 34.0


def test_an_init_during_the_grace_period_wins():
    started = threading.Event()

    def metadata(_port):
        raise AssertionError("asked daprd although it connected")

    clock = Clock()
    shutdown = Shutdown(clock, 2.0)
    original_wait = shutdown.wait

    def wait(timeout=None):
        if clock.now >= 4.0:
            started.set()
        return original_wait(timeout)

    shutdown.wait = wait
    assert not rejoin.watch(
        "bindings.smb", started, shutdown, 3500, metadata=metadata, stop_sidecar=lambda _p: None, clock=clock
    )
