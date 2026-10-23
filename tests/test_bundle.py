import io
import json
import tarfile

import pytest

from model_access.bundle import (
    MANIFEST_NAME,
    BundleError,
    build_checkpoint_bundle,
    read_bundle_manifest,
)


def _checkpoint(tmp_path, files=None):
    src = tmp_path / "ckpt"
    src.mkdir()
    payload = files if files is not None else {"model.safetensors": b"weights", "config.json": b"{}"}
    for name, content in payload.items():
        (src / name).write_bytes(content)
    return src


def test_manifest_is_the_first_member_and_lists_every_file(tmp_path):
    src = _checkpoint(tmp_path)
    result = build_checkpoint_bundle(src, tmp_path / "bundle.tar.gz")

    with tarfile.open(result.path, "r:gz") as archive:
        members = archive.getnames()
    assert members[0] == MANIFEST_NAME
    assert members[1:] == ["config.json", "model.safetensors"]

    entries = read_bundle_manifest(result.path)
    assert [e.path for e in entries] == ["config.json", "model.safetensors"]
    assert {e.path: e.size for e in entries}["model.safetensors"] == len(b"weights")


def test_manifest_hashes_match_the_payload(tmp_path):
    src = _checkpoint(tmp_path, {"a.bin": b"abc"})
    result = build_checkpoint_bundle(src, tmp_path / "b.tar.gz")
    entry = read_bundle_manifest(result.path)[0]
    assert entry.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_download_leftovers_are_skipped(tmp_path):
    src = _checkpoint(
        tmp_path,
        {
            "model.safetensors": b"w",
            "model-00002.safetensors.incomplete": b"partial",
            "._junk": b"apple double",
            "notes.tmp": b"tmp",
        },
    )
    result = build_checkpoint_bundle(src, tmp_path / "b.tar.gz")
    assert [e.path for e in result.entries] == ["model.safetensors"]


def test_subdirectories_are_ignored_not_fatal(tmp_path):
    src = _checkpoint(tmp_path)
    (src / "runs").mkdir()
    (src / "runs" / "events.out").write_bytes(b"x")
    result = build_checkpoint_bundle(src, tmp_path / "b.tar.gz")
    assert [e.path for e in result.entries] == ["config.json", "model.safetensors"]


def test_empty_directory_is_rejected(tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    with pytest.raises(BundleError, match="no payload files"):
        build_checkpoint_bundle(src, tmp_path / "b.tar.gz")


def test_missing_source_is_rejected(tmp_path):
    with pytest.raises(BundleError, match="not a directory"):
        build_checkpoint_bundle(tmp_path / "nope", tmp_path / "b.tar.gz")


def test_uncompressed_bundle_round_trips(tmp_path):
    src = _checkpoint(tmp_path)
    result = build_checkpoint_bundle(src, tmp_path / "b.tar.gz", compresslevel=0)
    assert [e.path for e in read_bundle_manifest(result.path)] == ["config.json", "model.safetensors"]


def test_manifest_not_first_is_rejected(tmp_path):
    # A hand-rolled archive with the payload before the manifest: a streaming
    # restorer could not have found the manifest, so the reader must say so.
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        first = tmp_path / "payload.bin"
        first.write_bytes(b"x")
        archive.add(first, arcname="payload.bin")
        info = tarfile.TarInfo(MANIFEST_NAME)
        payload = json.dumps({"version": 1, "files": [{"path": "payload.bin", "size": 1, "sha256": "0" * 64}]}).encode()
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with pytest.raises(BundleError, match="must be first"):
        read_bundle_manifest(path)


def test_unknown_manifest_version_is_rejected(tmp_path):
    path = tmp_path / "v9.tar.gz"
    payload = json.dumps({"version": 9, "files": [{"path": "a", "size": 1, "sha256": "0" * 64}]}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with pytest.raises(BundleError, match="unsupported manifest version"):
        read_bundle_manifest(path)


def test_manifest_without_files_is_rejected(tmp_path):
    path = tmp_path / "empty-manifest.tar.gz"
    payload = json.dumps({"version": 1, "files": []}).encode()
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with pytest.raises(BundleError, match="lists no files"):
        read_bundle_manifest(path)
