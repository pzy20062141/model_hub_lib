"""Checkpoint bundles: the single-file envelope for a multi-file model directory.

Model artifacts in the platform are single objects. Training checkpoints and
merged weights are directories (sharded safetensors + config + tokenizer), so
they need an envelope before they can cross an API that only moves one object
per artifact.

The envelope is a tar archive whose **first** entry is a JSON manifest listing
every payload file with its size and sha256. Consumers restore it by reading the
manifest, then verifying each extracted file. Format rules the restorer relies
on:

* the manifest must be the first member — a restorer that streams cannot seek
  back to find it later
* every payload entry must be a regular file with a relative path — links and
  absolute paths have no meaning once the archive is untrusted
* names are stored verbatim, so the caller decides whether to flatten a
  directory or keep a layout

The format string ``checkpoint-tar-gzip`` is what a platform stores on the
artifact to mark it as a bundle rather than a plain weights file.

Bundling large weights is I/O bound, not CPU bound: safetensors are already
entropy-dense, so compression buys little and costs a lot. ``compresslevel``
defaults to 1 for that reason and may be set to 0 to store without compression.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Archive member that carries the manifest. Must stay first.
MANIFEST_NAME = "vela-checkpoint-manifest.json"

#: Value a platform stores to mark an artifact as this envelope.
BUNDLE_FORMAT = "checkpoint-tar-gzip"

#: Manifest schema version written by :func:`build_checkpoint_bundle`.
MANIFEST_VERSION = 1

#: Download leftovers that never belong in a bundle (partial shards, OS metadata).
DEFAULT_SKIP_SUFFIXES: tuple[str, ...] = (".incomplete", ".tmp", ".part", ".DS_Store")


class BundleError(ValueError):
    """A directory cannot be bundled, or an archive is not a valid bundle."""


@dataclass(frozen=True)
class BundleEntry:
    """One payload file as recorded in the manifest."""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BundleResult:
    """Outcome of :func:`build_checkpoint_bundle`."""

    path: Path
    size: int
    entries: tuple[BundleEntry, ...]

    @property
    def file_count(self) -> int:
        return len(self.entries)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_files(source: Path, skip_suffixes: Sequence[str]) -> list[Path]:
    """Regular files directly under ``source``, minus download leftovers."""
    files: list[Path] = []
    for name in sorted(os.listdir(source)):
        candidate = source / name
        if not candidate.is_file():
            continue
        if name.endswith(tuple(skip_suffixes)) or name.startswith("._"):
            continue
        files.append(candidate)
    return files


def build_checkpoint_bundle(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    compresslevel: int = 1,
    skip_suffixes: Sequence[str] = DEFAULT_SKIP_SUFFIXES,
) -> BundleResult:
    """Pack the files directly under ``source`` into a bundle at ``destination``.

    Only direct children are taken: a checkpoint directory is flat by
    convention, and following nested trees would silently change what the
    restorer sees. Subdirectories are ignored, not an error — training outputs
    often carry ``runs/`` or ``.cache/`` beside the weights.

    Raises :class:`BundleError` when the source is not a directory or holds no
    payload files.
    """
    src = Path(source)
    dest = Path(destination)
    if not src.is_dir():
        raise BundleError(f"not a directory: {src}")

    files = _payload_files(src, skip_suffixes)
    if not files:
        raise BundleError(f"no payload files under {src}")

    entries = tuple(
        BundleEntry(path=f.name, size=f.stat().st_size, sha256=_sha256_of(f)) for f in files
    )
    manifest = json.dumps(
        {"version": MANIFEST_VERSION, "files": [e.__dict__ for e in entries]},
        ensure_ascii=False,
    ).encode()

    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz", compresslevel=compresslevel) as archive:
        header = tarfile.TarInfo(MANIFEST_NAME)
        header.size = len(manifest)
        header.mode = 0o644
        archive.addfile(header, io.BytesIO(manifest))
        for entry in entries:
            archive.add(src / entry.path, arcname=entry.path)

    return BundleResult(path=dest, size=dest.stat().st_size, entries=entries)


def read_bundle_manifest(bundle: str | os.PathLike[str]) -> tuple[BundleEntry, ...]:
    """Read and validate the manifest of an existing bundle.

    Cheap (it stops at the first non-manifest member) and is meant for checking
    an archive before shipping it, not for restoring it.
    """
    path = Path(bundle)
    with tarfile.open(path, "r:*") as archive:
        first = archive.next()
        if first is None or first.name != MANIFEST_NAME:
            raise BundleError(f"bundle manifest must be first, got {first.name if first else None!r}")
        stream = archive.extractfile(first)
        if stream is None:
            raise BundleError("bundle manifest is not a regular file")
        try:
            payload = json.loads(stream.read())
        except ValueError as exc:
            raise BundleError("bundle manifest is not valid JSON") from exc

    if payload.get("version") != MANIFEST_VERSION:
        raise BundleError(f"unsupported manifest version: {payload.get('version')!r}")
    entries: Iterable[dict[str, object]] = payload.get("files") or ()
    result = []
    for raw in entries:
        try:
            result.append(
                BundleEntry(path=str(raw["path"]), size=int(raw["size"]), sha256=str(raw["sha256"]))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError("bundle manifest entry is malformed") from exc
    if not result:
        raise BundleError("bundle manifest lists no files")
    return tuple(result)


def _main(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m model_access.bundle <source-dir> <destination.tar.gz>")
        return 2
    try:
        result = build_checkpoint_bundle(argv[1], argv[2])
    except BundleError as exc:
        print(f"error: {exc}")
        return 1
    print(f"bundle: {result.path}  {result.size / 1e9:.2f} GB  {result.file_count} files")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI
    import sys

    raise SystemExit(_main(sys.argv))
