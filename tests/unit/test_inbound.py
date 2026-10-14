"""The read side of the completion contract."""

from __future__ import annotations

import time

import pytest
from conftest import make_cfg

from dapr_smb_binding.errors import Kind, SmbError
from dapr_smb_binding.inbound import Inbound


class Clock:
    # Starts at the real time: stale-claim checks compare it with real file times.
    def __init__(self, t: float | None = None):
        self.t = time.time() if t is None else t

    def __call__(self) -> float:
        return self.t


def inbound(lfs, clock=None, instance="pod-a", max_file_size=1 << 20, **cfg):
    c = make_cfg(inboundPath="in", **cfg)
    return Inbound(
        lfs, c.read, c.write, instance_id=instance, max_file_size=max_file_size, clock=clock or Clock()
    )


def deliver_all(ib, ok=True):
    """Poll, load and complete everything; returns the delivered file names."""
    out = []
    for c in ib.poll():
        m = ib.load(c)
        if m is None:
            continue
        out.append(m.claimed.name)
        ib.complete(c, ok=ok, error="" if ok else "boom")
    return out


# ---------------------------------------------------------------- defaults


def test_default_claims_by_rename_then_moves_to_processed(lfs):
    lfs.put("in/a.xml", b"<a/>")
    ib = inbound(lfs)
    [c] = ib.poll()
    assert c.path == "in/processing/pod-a/a.xml"
    assert lfs.files("in") == ["processing/pod-a/a.xml"]
    m = ib.load(c)
    assert m.data == b"<a/>"
    assert m.metadata["fileName"] == "a.xml"
    assert m.content_type == "application/xml"
    ib.complete(c, ok=True)
    assert lfs.files("in") == ["processed/a.xml"]


def test_file_pattern_and_ignore(lfs):
    for n in ("a.xml", "b.txt", "c.xml.tmp", "~d.xml", "e.xml.lock"):
        lfs.put(f"in/{n}")
    ib = inbound(lfs, filePattern="*.xml,*.txt", ignorePatterns="~*")
    assert sorted(deliver_all(ib)) == ["a.xml", "b.txt"]


def test_own_temp_pattern_always_ignored_even_with_star(lfs):
    lfs.put("in/a.xml.tmp")
    lfs.put("in/b.xml")
    assert deliver_all(inbound(lfs)) == ["b.xml"]


def test_directories_never_delivered(lfs):
    (lfs.root / "in/sub").mkdir(parents=True)
    lfs.put("in/a.xml")
    assert deliver_all(inbound(lfs)) == ["a.xml"]


# ---------------------------------------------------------------- partner still writing


def test_sharing_violation_skips_until_partner_closes(lfs):
    lfs.put("in/a.xml", b"partial")
    lfs.open_by_partner.add("in/a.xml")
    ib = inbound(lfs)
    assert ib.poll() == []
    assert lfs.files("in") == ["a.xml"]

    (lfs.root / "in/a.xml").write_bytes(b"complete")
    lfs.open_by_partner.clear()
    [c] = ib.poll()
    assert ib.load(c).data == b"complete"


def test_exclusive_read_after_claim_retries_next_poll(lfs):
    # Partner opened with delete-sharing (rename allowed) but no read-sharing.
    lfs.put("in/a.xml")
    ib = inbound(lfs)
    [c] = ib.poll()
    lfs.open_by_partner.add(c.path)
    assert ib.load(c) is None
    lfs.open_by_partner.clear()
    # Stays claimed and comes back on the next poll.
    [again] = ib.poll()
    assert again.path == "in/processing/pod-a/a.xml"
    assert ib.load(again) is not None


def test_two_replicas_only_one_wins(lfs):
    lfs.put("in/a.xml")
    a, b = inbound(lfs, instance="pod-a"), inbound(lfs, instance="pod-b")
    assert [c.path for c in a.poll()] == ["in/processing/pod-a/a.xml"]
    assert b.poll() == []


def test_replica_losing_the_rename_race_skips(lfs):
    lfs.put("in/a.xml")
    ib = inbound(lfs)
    orig = lfs.rename

    def racing_rename(src, dst):
        (lfs.root / src).unlink()  # another replica won
        return orig(src, dst)

    lfs.rename = racing_rename
    assert ib.poll() == []


# ---------------------------------------------------------------- requireMarkerPattern


def test_marker_required(lfs):
    lfs.put("in/a.xml")
    ib = inbound(lfs, requireMarkerPattern="{name}.done")
    assert ib.poll() == []
    lfs.put("in/a.xml.done")
    assert deliver_all(ib) == ["a.xml"]
    # marker is never delivered itself, and is cleaned up afterwards
    assert lfs.files("in") == ["processed/a.xml"]


# ---------------------------------------------------------------- stableFor


def test_stable_for(lfs):
    clock = Clock()
    lfs.put("in/a.xml", b"1")
    ib = inbound(lfs, clock=clock, stableFor="30s")
    assert ib.poll() == []  # first sighting
    clock.t += 10
    assert ib.poll() == []  # not stable long enough
    lfs.put("in/a.xml", b"12")  # grew: the clock restarts
    clock.t += 25
    assert ib.poll() == []
    clock.t += 31
    assert [c.name for c in ib.poll()] == ["a.xml"]


# ---------------------------------------------------------------- afterRead / errors


def test_after_read_delete(lfs):
    lfs.put("in/a.xml")
    deliver_all(inbound(lfs, afterRead="delete"))
    assert lfs.files("in") == []


def test_after_read_none_leaves_in_processing(lfs):
    lfs.put("in/a.xml")
    ib = inbound(lfs, afterRead="none")
    assert deliver_all(ib) == ["a.xml"]
    assert lfs.files("in") == ["processing/pod-a/a.xml"]
    assert deliver_all(ib) == []


def test_processed_name_collision_keeps_both(lfs):
    lfs.put("in/processed/a.xml", b"old")
    lfs.put("in/a.xml", b"new")
    deliver_all(inbound(lfs))
    assert lfs.files("in") == ["processed/a.xml", "processed/a_1.xml"]


def test_app_error_moves_to_error_folder(lfs):
    lfs.put("in/a.xml")
    deliver_all(inbound(lfs), ok=False)
    assert lfs.files("in") == ["error/a.xml"]


def test_app_error_without_error_folder_retries(lfs):
    lfs.put("in/a.xml")
    ib = inbound(lfs, errorFolder="")
    assert deliver_all(ib, ok=False) == ["a.xml"]
    assert lfs.files("in") == ["a.xml"]
    assert deliver_all(ib) == ["a.xml"]


def test_oversize_goes_to_error_folder(lfs):
    lfs.put("in/big.bin", b"x" * 100)
    ib = inbound(lfs, max_file_size=10)
    [c] = ib.poll()
    assert ib.load(c) is None
    assert lfs.files("in") == ["error/big.bin"]


# ---------------------------------------------------------------- claimMode none


def test_claim_mode_none_reads_in_place_once(lfs):
    lfs.put("in/a.xml")
    ib = inbound(lfs, claimMode="none", afterRead="none")
    assert deliver_all(ib) == ["a.xml"]
    assert deliver_all(ib) == []  # not delivered twice
    assert lfs.files("in") == ["a.xml"]


def test_claim_mode_none_exclusive_read_skips_open_file(lfs):
    lfs.put("in/a.xml")
    lfs.open_by_partner.add("in/a.xml")
    ib = inbound(lfs, claimMode="none")
    assert deliver_all(ib) == []
    lfs.open_by_partner.clear()
    assert deliver_all(ib) == ["a.xml"]
    assert lfs.files("in") == ["processed/a.xml"]


# ---------------------------------------------------------------- crash recovery


def test_recover_redelivers_own_processing_folder_only(lfs):
    lfs.put("in/processing/pod-a/a.xml")
    lfs.put("in/processing/pod-b/b.xml")
    ib = inbound(lfs, instance="pod-a")
    rec = ib.recover()
    assert [c.name for c in rec] == ["a.xml"]
    assert rec[0].recovered


def test_recover_with_no_processing_folder(lfs):
    assert inbound(lfs).recover() == []


def test_custom_folder_names(lfs):
    lfs.put("in/a.xml")
    lfs.put("in/b.xml")
    ib = inbound(lfs, processingFolder=".claim", processedFolder="done", errorFolder="bad")
    [a, b] = ib.poll()
    assert a.path == "in/.claim/pod-a/a.xml"
    ib.load(a)
    ib.complete(a, ok=True)
    ib.load(b)
    ib.complete(b, ok=False)
    assert lfs.files("in") == ["bad/b.xml", "done/a.xml"]


def test_list_failure_propagates(lfs):
    lfs.fail_next = [SmbError(Kind.ACCESS_DENIED, "denied")]
    with pytest.raises(SmbError):
        inbound(lfs).poll()


# ---------------------------------------------------------------- stale claims (replica gone)


def test_heartbeat_written_in_own_processing_folder(lfs):
    ib = inbound(lfs)
    ib.heartbeat()
    assert lfs.files("in", heartbeats=True) == ["processing/pod-a/.heartbeat"]


def test_recover_ignores_heartbeat(lfs):
    ib = inbound(lfs)
    ib.heartbeat()
    assert ib.recover() == []


def test_claims_of_a_gone_replica_are_returned(lfs):
    lfs.put("in/processing/old-pod/a.xml", b"<a/>", age=3600)
    lfs.put("in/processing/old-pod/.heartbeat", age=3600)
    ib = inbound(lfs, instance="new-pod", staleClaimAge="15m")
    # Returned to inbound, then claimed by this replica in the same poll.
    assert [c.path for c in ib.poll()] == ["in/processing/new-pod/a.xml"]
    assert not (lfs.root / "in/processing/old-pod").exists()


def test_live_replica_keeps_its_claims(lfs):
    lfs.put("in/processing/pod-b/a.xml", age=3600)  # the partner wrote it long ago...
    lfs.put("in/processing/pod-b/.heartbeat")  # ...but pod-b is alive
    assert inbound(lfs, staleClaimAge="15m").poll() == []
    assert lfs.files("in") == ["processing/pod-b/a.xml"]


def test_folder_without_heartbeat_uses_folder_time(lfs):
    lfs.put("in/processing/pod-b/a.xml", age=3600)  # file time is the partner's: ignored
    # The folder was just created (claimed now), so pod-b counts as alive.
    assert inbound(lfs, staleClaimAge="15m").poll() == []


def test_folder_without_heartbeat_and_old_is_stale(lfs):
    import os

    lfs.put("in/processing/pod-b/a.xml", age=3600)
    d = lfs.root / "in/processing/pod-b"
    t = time.time() - 3600
    os.utime(d, (t, t))
    assert [c.name for c in inbound(lfs, staleClaimAge="15m").poll()] == ["a.xml"]


def test_returned_claim_does_not_overwrite_a_resent_file(lfs):
    lfs.put("in/processing/old-pod/a.xml", b"old", age=3600)
    lfs.put("in/processing/old-pod/.heartbeat", age=3600)
    lfs.put("in/a.xml", b"resent")
    ib = inbound(lfs, instance="new-pod")
    ib.sweep_stale_claims()
    assert (lfs.root / "in/a.xml").read_bytes() == b"resent"
    assert (lfs.root / "in/processing/old-pod/a.xml").exists()


def test_stale_claim_age_off(lfs):
    lfs.put("in/processing/old-pod/a.xml", age=3600)
    lfs.put("in/processing/old-pod/.heartbeat", age=3600)
    assert inbound(lfs, staleClaimAge="").sweep_stale_claims() == 0


def test_own_folder_never_swept(lfs):
    lfs.put("in/processing/pod-a/a.xml", age=3600)
    lfs.put("in/processing/pod-a/.heartbeat", age=3600)
    assert inbound(lfs).sweep_stale_claims() == 0


def test_sweep_runs_at_most_once_a_minute(lfs):
    (lfs.root / "in").mkdir()
    clock = Clock()
    ib = inbound(lfs, clock=clock)
    ib.poll()
    ib.poll()
    lists = [p for op, p in lfs.calls if op == "list" and p == "in/processing"]
    assert len(lists) == 1
    clock.t += 61
    ib.poll()
    assert len([p for op, p in lfs.calls if op == "list" and p == "in/processing"]) == 2


def test_recover_clears_the_retry_list(lfs):
    lfs.put("in/a.xml")
    ib = inbound(lfs)
    [c] = ib.poll()
    lfs.open_by_partner.add(c.path)
    assert ib.load(c) is None  # now waiting for a retry
    lfs.open_by_partner.clear()
    # A new stream: recover() finds it in the processing folder...
    assert [x.name for x in ib.recover()] == ["a.xml"]
    # ...and the next poll does not hand it out a second time.
    assert ib.poll() == []
