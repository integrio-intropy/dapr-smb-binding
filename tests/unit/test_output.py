"""Output operations and the temp-file + rename write."""

from __future__ import annotations

import json

import pytest
from conftest import make_cfg

from dapr_smb_binding.errors import Kind, SmbError
from dapr_smb_binding.output import OPERATIONS, OutputBinding


def binding(lfs, **cfg):
    return OutputBinding(make_cfg(**cfg), lfs)


def test_operations():
    assert OPERATIONS == ("create", "get", "list", "delete", "rename")


def test_roundtrip_with_sftp_shapes(lfs):
    b = binding(lfs)
    r = b.invoke("create", b"hello", {"fileName": "a.xml"})
    assert json.loads(r.data) == {"fileName": "a.xml"}
    assert r.metadata == {"fileName": "a.xml", "filePath": "out/a.xml"}

    assert b.invoke("get", b"", {"fileName": "a.xml"}).data == b"hello"
    [entry] = json.loads(b.invoke("list", b"", {}).data)
    assert (entry["fileName"], entry["isDirectory"], entry["size"]) == ("a.xml", False, 5)

    b.invoke("delete", b"", {"fileName": "a.xml"})
    assert lfs.files() == []


def test_create_writes_temp_then_renames(lfs):
    binding(lfs).invoke("create", b"x", {"fileName": "a.xml"})
    assert [c for c in lfs.calls if c[0] in ("write", "rename")] == [
        ("write", "out/a.xml.tmp"),
        ("rename", "out/a.xml.tmp"),
    ]
    assert lfs.files() == ["out/a.xml"]


def test_custom_temp_pattern(lfs):
    binding(lfs, tempFileNamePattern=".{name}").invoke("create", b"x", {"fileName": "a.xml"})
    assert ("write", "out/.a.xml") in lfs.calls


def test_create_makes_parent_folders(lfs):
    r = binding(lfs).invoke("create", b"x", {"fileName": "sub/a.xml"})
    assert json.loads(r.data) == {"fileName": "sub/a.xml"}
    assert lfs.files() == ["out/sub/a.xml"]


def test_create_never_overwrites_and_cleans_up(lfs):
    lfs.put("out/a.xml", b"old")
    with pytest.raises(SmbError) as e:
        binding(lfs).invoke("create", b"new", {"fileName": "a.xml"})
    assert e.value.kind is Kind.EXISTS
    assert lfs.files() == ["out/a.xml"]  # temp file removed
    assert (lfs.root / "out/a.xml").read_bytes() == b"old"


def test_leftover_temp_is_overwritten(lfs):
    lfs.put("out/a.xml.tmp", b"partial, from a crash")
    binding(lfs).invoke("create", b"complete", {"fileName": "a.xml"})
    assert lfs.files() == ["out/a.xml"]
    assert (lfs.root / "out/a.xml").read_bytes() == b"complete"


def test_temp_another_writer_has_open_is_left_alone(lfs):
    lfs.put("out/a.xml.tmp", b"someone else's, still being written")
    lfs.open_by_partner.add("out/a.xml.tmp")
    with pytest.raises(SmbError) as e:
        binding(lfs).invoke("create", b"x", {"fileName": "a.xml"})
    assert e.value.kind is Kind.SHARING_VIOLATION
    assert lfs.files() == ["out/a.xml.tmp"]
    assert (lfs.root / "out/a.xml.tmp").read_bytes() == b"someone else's, still being written"


def test_rename_never_overwrites(lfs):
    lfs.put("out/a.xml")
    lfs.put("out/b.xml")
    with pytest.raises(SmbError) as e:
        binding(lfs).invoke("rename", b"", {"fileName": "a.xml", "newFileName": "b.xml"})
    assert e.value.kind is Kind.EXISTS
    r = binding(lfs).invoke("rename", b"", {"fileName": "a.xml", "newFileName": "archive/a.xml"})
    assert json.loads(r.data) == {"fileName": "archive/a.xml"}


@pytest.mark.parametrize("op", ["create", "get", "delete", "rename"])
def test_file_name_required(lfs, op):
    with pytest.raises(SmbError, match="fileName is missing"):
        binding(lfs).invoke(op, b"", {})


def test_metadata_keys_case_insensitive(lfs):
    binding(lfs).invoke("create", b"x", {"FILENAME": "a.xml"})
    assert lfs.files() == ["out/a.xml"]


def test_unsupported_operation(lfs):
    with pytest.raises(SmbError, match='unsupported operation "move"'):
        binding(lfs).invoke("move", b"", {})
