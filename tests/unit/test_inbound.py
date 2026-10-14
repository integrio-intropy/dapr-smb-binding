"""The input side: exclusive read in place, move after the app's answer."""

from __future__ import annotations

from conftest import make_cfg

from dapr_smb_binding.inbound import Inbound


def inbound(lfs, max_file_size=1 << 20, **cfg):
    return Inbound(make_cfg(inboundPath="in", **cfg), lfs, max_file_size=max_file_size)


def deliver(ib, ok=True):
    out = []
    for e in ib.poll():
        m = ib.load(e)
        if m:
            out.append(m.name)
            ib.complete(m.name, ok=ok)
    return out


def test_read_in_place_then_processed(lfs):
    lfs.put("in/a.xml", b"<a/>")
    ib = inbound(lfs)
    [e] = ib.poll()
    m = ib.load(e)
    assert (m.data, m.metadata["fileName"], m.content_type) == (b"<a/>", "a.xml", "application/xml")
    assert lfs.files("in") == ["a.xml"]  # untouched until the app answers
    ib.complete("a.xml", ok=True)
    assert lfs.files("in") == ["processed/a.xml"]


def test_rejected_goes_to_error(lfs):
    lfs.put("in/a.xml")
    deliver(inbound(lfs), ok=False)
    assert lfs.files("in") == ["error/a.xml"]


def test_partner_still_writing_is_skipped(lfs):
    lfs.put("in/a.xml", b"partial")
    lfs.open_by_partner.add("in/a.xml")
    ib = inbound(lfs)
    assert deliver(ib) == []
    assert lfs.files("in") == ["a.xml"]
    lfs.open_by_partner.clear()
    assert deliver(ib) == ["a.xml"]


def test_crash_before_answer_redelivers(lfs):
    lfs.put("in/a.xml")
    ib = inbound(lfs)
    [e] = ib.poll()
    ib.load(e)  # ...and the process dies before complete()
    assert [x.name for x in inbound(lfs).poll()] == ["a.xml"]


def test_patterns_and_own_temp_files(lfs):
    for n in ("a.xml", "b.txt", "c.xml.tmp", "~d.xml"):
        lfs.put(f"in/{n}")
    (lfs.root / "in/sub").mkdir()
    assert sorted(deliver(inbound(lfs, filePattern="*.xml,*.txt", ignorePatterns="~*"))) == ["a.xml", "b.txt"]


def test_oldest_first(lfs):
    lfs.put("in/new.xml")
    lfs.put("in/old.xml", age=60)
    assert [e.name for e in inbound(lfs).poll()] == ["old.xml", "new.xml"]


def test_name_reuse_keeps_both(lfs):
    lfs.put("in/processed/a.xml", b"yesterday")
    lfs.put("in/a.xml", b"today")
    deliver(inbound(lfs))
    assert lfs.files("in") == ["processed/a.xml", "processed/a_1.xml"]


def test_oversize_goes_to_error(lfs):
    lfs.put("in/big.bin", b"x" * 100)
    assert deliver(inbound(lfs, max_file_size=10)) == []
    assert lfs.files("in") == ["error/big.bin"]


def test_custom_folders(lfs):
    lfs.put("in/a.xml")
    deliver(inbound(lfs, processedFolder="done"))
    assert lfs.files("in") == ["done/a.xml"]
