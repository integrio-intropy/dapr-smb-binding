"""Output operations and the write side of the completion contract."""

from __future__ import annotations

import json

import pytest
from conftest import make_cfg

from dapr_smb_binding.errors import Kind, SmbError
from dapr_smb_binding.output import OPERATIONS, OutputBinding


def binding(lfs, **cfg):
    return OutputBinding(make_cfg(**cfg), lfs)


def create(b, name, data=b"<a/>", **md):
    return b.invoke("create", data, {"fileName": name, **md})


# ---------------------------------------------------------------- SFTP-compatible shapes


def test_operations_listed():
    assert OPERATIONS == ("create", "get", "list", "delete", "rename")


def test_create_get_list_delete_roundtrip(lfs):
    b = binding(lfs)
    r = create(b, "a.xml", b"hello")
    assert json.loads(r.data) == {"fileName": "a.xml"}
    assert r.metadata == {"fileName": "a.xml", "filePath": "out/a.xml"}
    assert r.content_type == "application/json"

    g = b.invoke("get", b"", {"fileName": "a.xml"})
    assert g.data == b"hello"
    assert g.metadata["size"] == "5"

    entries = json.loads(b.invoke("list", b"", {}).data)
    assert [(e["fileName"], e["isDirectory"], e["size"]) for e in entries] == [("a.xml", False, 5)]
    assert entries[0]["modTime"].endswith("Z")

    d = b.invoke("delete", b"", {"fileName": "a.xml"})
    assert d.data == b""
    assert lfs.files() == []


def test_create_makes_parent_dirs_like_sftp(lfs):
    b = binding(lfs)
    r = create(b, "sub/dir/a.xml")
    assert json.loads(r.data) == {"fileName": "sub/dir/a.xml"}
    assert lfs.files() == ["out/sub/dir/a.xml"]


def test_request_root_path_override_like_sftp(lfs):
    b = binding(lfs)
    create(b, "a.xml", rootPath="other")
    assert lfs.files() == ["other/a.xml"]


def test_metadata_keys_case_insensitive(lfs):
    b = binding(lfs)
    b.invoke("create", b"x", {"FILENAME": "a.xml"})
    assert lfs.files() == ["out/a.xml"]


@pytest.mark.parametrize("op", ["create", "get", "delete", "rename"])
def test_file_name_required(lfs, op):
    with pytest.raises(SmbError, match="fileName is missing"):
        binding(lfs).invoke(op, b"", {})


def test_unsupported_operation(lfs):
    with pytest.raises(SmbError, match='unsupported operation "move"'):
        binding(lfs).invoke("move", b"", {})


def test_get_missing_is_not_found(lfs):
    with pytest.raises(SmbError) as e:
        binding(lfs).invoke("get", b"", {"fileName": "nope"})
    assert e.value.kind is Kind.NOT_FOUND


# ---------------------------------------------------------------- tempFileMode


def test_name_mode_writes_temp_then_renames(lfs):
    b = binding(lfs)
    create(b, "a.xml", b"data")
    ops = [(op, p) for op, p in lfs.calls if op in ("write", "rename")]
    assert ops == [("write", "out/a.xml.tmp"), ("rename", "out/a.xml.tmp")]
    assert lfs.files() == ["out/a.xml"]


def test_custom_temp_pattern(lfs):
    b = binding(lfs, tempFileNamePattern="~{name}")
    create(b, "a.xml")
    assert ("write", "out/~a.xml") in lfs.calls


def test_folder_mode_stages_in_temp_folder(lfs):
    b = binding(lfs, tempFileMode="folder", tempFolder="_staging")
    create(b, "a.xml")
    assert ("write", "out/_staging/a.xml") in lfs.calls
    assert lfs.files() == ["out/a.xml"]


def test_none_mode_writes_directly(lfs):
    b = binding(lfs, tempFileMode="none")
    create(b, "a.xml")
    assert [p for op, p in lfs.calls if op == "write"] == ["out/a.xml"]
    assert not any(op == "rename" for op, _ in lfs.calls)


def test_temp_file_is_removed_when_rename_fails(lfs):
    lfs.put("out/a.xml", b"old")
    b = binding(lfs)
    with pytest.raises(SmbError) as e:
        create(b, "a.xml", b"new")
    assert e.value.kind is Kind.EXISTS
    assert lfs.files() == ["out/a.xml"]
    assert (lfs.root / "out/a.xml").read_bytes() == b"old"


# ---------------------------------------------------------------- onTargetExists


@pytest.mark.parametrize("mode", ["name", "folder", "none"])
def test_fail_never_overwrites(lfs, mode):
    lfs.put("out/a.xml", b"old")
    with pytest.raises(SmbError) as e:
        create(binding(lfs, tempFileMode=mode), "a.xml", b"new")
    assert e.value.kind is Kind.EXISTS
    assert (lfs.root / "out/a.xml").read_bytes() == b"old"


@pytest.mark.parametrize("mode", ["name", "folder", "none"])
def test_overwrite(lfs, mode):
    lfs.put("out/a.xml", b"old")
    create(binding(lfs, tempFileMode=mode, onTargetExists="overwrite"), "a.xml", b"new")
    assert (lfs.root / "out/a.xml").read_bytes() == b"new"


@pytest.mark.parametrize("mode", ["name", "folder", "none"])
def test_unique_name(lfs, mode):
    lfs.put("out/a.xml", b"old")
    lfs.put("out/a_1.xml", b"old1")
    r = create(binding(lfs, tempFileMode=mode, onTargetExists="uniqueName"), "a.xml", b"new")
    assert json.loads(r.data) == {"fileName": "a_2.xml"}
    assert (lfs.root / "out/a_2.xml").read_bytes() == b"new"
    assert (lfs.root / "out/a.xml").read_bytes() == b"old"


def test_on_target_exists_per_request(lfs):
    lfs.put("out/a.xml", b"old")
    create(binding(lfs), "a.xml", b"new", onTargetExists="overwrite")
    assert (lfs.root / "out/a.xml").read_bytes() == b"new"


# ---------------------------------------------------------------- staleTempFileAge


def test_crash_between_write_and_rename_blocks_without_cleanup(lfs):
    lfs.put("out/a.xml.tmp", b"partial", age=7200)
    with pytest.raises(SmbError, match="staleTempFileAge"):
        create(binding(lfs), "a.xml")


def test_stale_temp_of_same_name_is_replaced(lfs):
    lfs.put("out/a.xml.tmp", b"partial", age=7200)
    create(binding(lfs, staleTempFileAge="1h"), "a.xml", b"full")
    assert lfs.files() == ["out/a.xml"]
    assert (lfs.root / "out/a.xml").read_bytes() == b"full"


def test_fresh_temp_of_same_name_is_not_touched(lfs):
    lfs.put("out/a.xml.tmp", b"in progress")
    with pytest.raises(SmbError) as e:
        create(binding(lfs, staleTempFileAge="1h"), "a.xml")
    assert e.value.kind is Kind.EXISTS
    assert (lfs.root / "out/a.xml.tmp").read_bytes() == b"in progress"


def test_cleanup_removes_only_our_old_temp_files(lfs):
    lfs.put("out/orphan.xml.tmp", age=7200)
    lfs.put("out/young.xml.tmp", age=10)
    lfs.put("out/partner.lock", age=7200)
    create(binding(lfs, staleTempFileAge="1h"), "b.xml")
    assert lfs.files() == ["out/b.xml", "out/partner.lock", "out/young.xml.tmp"]


def test_cleanup_in_folder_mode(lfs):
    lfs.put("out/_staging/orphan.xml", age=7200)
    lfs.put("out/keep.xml", age=7200)
    create(binding(lfs, tempFileMode="folder", staleTempFileAge="1h"), "b.xml")
    assert lfs.files() == ["out/b.xml", "out/keep.xml"]


# ---------------------------------------------------------------- completionMarkerPattern


def test_marker_written_after_rename(lfs):
    create(binding(lfs, completionMarkerPattern="{name}.done"), "a.xml")
    writes = [p for op, p in lfs.calls if op in ("write", "rename")]
    assert writes == ["out/a.xml.tmp", "out/a.xml.tmp", "out/a.xml.done"]
    assert lfs.files() == ["out/a.xml", "out/a.xml.done"]


def test_marker_follows_unique_name(lfs):
    lfs.put("out/a.xml")
    create(binding(lfs, completionMarkerPattern="{name}.done", onTargetExists="uniqueName"), "a.xml")
    assert "out/a_1.xml.done" in lfs.files()


def _fail_marker_writes(lfs, times):
    orig = lfs.write
    left = [times]

    def write(path, data, *, exclusive_create):
        if path.endswith(".done") and left[0]:
            left[0] -= 1
            raise SmbError(Kind.ACCESS_DENIED, "denied", status=0xC0000022)
        return orig(path, data, exclusive_create=exclusive_create)

    lfs.write = write


def test_marker_retried_once(lfs):
    _fail_marker_writes(lfs, 1)
    create(binding(lfs, completionMarkerPattern="{name}.done"), "a.xml")
    assert lfs.files() == ["out/a.xml", "out/a.xml.done"]


def test_marker_failure_says_the_file_was_written(lfs):
    _fail_marker_writes(lfs, 2)
    with pytest.raises(SmbError) as e:
        create(binding(lfs, completionMarkerPattern="{name}.done"), "a.xml")
    msg = str(e.value)
    assert '"out/a.xml" was written' in msg
    assert "Do not resend" in msg
    assert e.value.kind is Kind.OTHER  # not EXISTS/NOT_FOUND: the caller must not branch on those
    assert lfs.files() == ["out/a.xml"]  # the file stays final; it is not rolled back


# ---------------------------------------------------------------- rename


def test_rename_refuses_to_overwrite(lfs):
    lfs.put("out/a.xml", b"a")
    lfs.put("out/b.xml", b"b")
    with pytest.raises(SmbError) as e:
        binding(lfs).invoke("rename", b"", {"fileName": "a.xml", "newFileName": "b.xml"})
    assert e.value.kind is Kind.EXISTS


def test_rename(lfs):
    lfs.put("out/a.xml", b"a")
    r = binding(lfs).invoke("rename", b"", {"fileName": "a.xml", "newFileName": "archive/a.xml"})
    assert json.loads(r.data) == {"fileName": "archive/a.xml"}
    assert lfs.files() == ["out/archive/a.xml"]


def test_rename_requires_new_file_name(lfs):
    with pytest.raises(SmbError, match="newFileName"):
        binding(lfs).invoke("rename", b"", {"fileName": "a.xml"})
