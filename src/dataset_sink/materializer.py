from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .errors import IntegrityError, ReleaseConflictError
from .manifest import Manifest, ManifestEntry
from .sources import SourceReader


@dataclass(frozen=True)
class MaterializationResult:
    dataset: str
    repository: str
    source_reference: str
    lakefs_commit: str
    lakefs_tag: Optional[str]
    paimon_snapshot_id: Optional[str]
    manifest_sha256: str
    file_count: int
    size_bytes: int
    release_path: str
    created_at: str
    status: str = "READY"


class Materializer:
    def __init__(self, target_root: Path, source: SourceReader, workers: int = 8) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.target_root = target_root.resolve()
        self.source = source
        self.workers = workers

    def materialize(
        self,
        *,
        dataset: str,
        repository: str,
        source_reference: str,
        commit_id: str,
        manifest: Manifest,
        lakefs_tag: Optional[str] = None,
        paimon_snapshot_id: Optional[str] = None,
    ) -> MaterializationResult:
        _validate_component(dataset, "dataset")
        _validate_component(commit_id, "commit_id")

        release_dir = self.target_root / dataset / commit_id
        lock_dir = self.target_root / ".locks" / dataset
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{commit_id}.lock"

        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            existing = self._load_existing(release_dir, manifest.sha256)
            if existing is not None:
                return existing

            return self._materialize_new(
                release_dir=release_dir,
                dataset=dataset,
                repository=repository,
                source_reference=source_reference,
                commit_id=commit_id,
                manifest=manifest,
                lakefs_tag=lakefs_tag,
                paimon_snapshot_id=paimon_snapshot_id,
            )

    def _materialize_new(
        self,
        *,
        release_dir: Path,
        dataset: str,
        repository: str,
        source_reference: str,
        commit_id: str,
        manifest: Manifest,
        lakefs_tag: Optional[str],
        paimon_snapshot_id: Optional[str],
    ) -> MaterializationResult:
        staging_parent = self.target_root / ".materializing" / dataset
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=f"{commit_id}.", dir=staging_parent))

        try:
            copied: Dict[str, int] = {}
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {
                    executor.submit(self._copy_entry, staging_dir, commit_id, entry): entry
                    for entry in manifest.entries
                }
                for future in as_completed(futures):
                    entry = futures[future]
                    copied[entry.target_path] = future.result()

            size_bytes = sum(copied.values())
            (staging_dir / "manifest.jsonl").write_bytes(manifest.raw_bytes)
            result = MaterializationResult(
                dataset=dataset,
                repository=repository,
                source_reference=source_reference,
                lakefs_commit=commit_id,
                lakefs_tag=lakefs_tag,
                paimon_snapshot_id=paimon_snapshot_id,
                manifest_sha256=manifest.sha256,
                file_count=len(manifest.entries),
                size_bytes=size_bytes,
                release_path=str(release_dir),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            _write_json(staging_dir / "release.json", asdict(result))

            release_dir.parent.mkdir(parents=True, exist_ok=True)
            if release_dir.exists():
                raise ReleaseConflictError(f"release appeared concurrently: {release_dir}")
            os.rename(staging_dir, release_dir)
            _write_json_atomic(
                release_dir / "_READY",
                {
                    "lakefs_commit": commit_id,
                    "manifest_sha256": manifest.sha256,
                    "status": "READY",
                },
            )
            return result
        except Exception:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            raise

    def _copy_entry(self, staging_dir: Path, commit_id: str, entry: ManifestEntry) -> int:
        destination = (staging_dir / entry.target_path).resolve()
        staging_root = staging_dir.resolve()
        if staging_root not in destination.parents:
            raise ValueError(f"target path escapes staging directory: {entry.target_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")

        digest = hashlib.sha256()
        size = 0
        try:
            with self.source.open(commit_id, entry) as source, partial.open("wb") as target:
                while True:
                    chunk = source.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())

            if entry.size_bytes is not None and size != entry.size_bytes:
                raise IntegrityError(
                    f"{entry.source_key}: expected {entry.size_bytes} bytes, received {size}"
                )
            actual_digest = digest.hexdigest()
            if entry.sha256 is not None and actual_digest != entry.sha256:
                raise IntegrityError(
                    f"{entry.source_key}: expected sha256 {entry.sha256}, received {actual_digest}"
                )
            os.replace(partial, destination)
            return size
        finally:
            if partial.exists():
                partial.unlink()

    @staticmethod
    def _load_existing(release_dir: Path, manifest_sha256: str) -> Optional[MaterializationResult]:
        if not release_dir.exists():
            return None
        release_file = release_dir / "release.json"
        ready_file = release_dir / "_READY"
        if not release_file.exists() or not ready_file.exists():
            raise ReleaseConflictError(f"incomplete immutable release already exists: {release_dir}")
        value = json.loads(release_file.read_text(encoding="utf-8"))
        if value.get("manifest_sha256") != manifest_sha256:
            raise ReleaseConflictError(
                f"release {release_dir} exists with a different manifest; immutable paths cannot be overwritten"
            )
        return MaterializationResult(**value)


def verify_release(path: Path, deep: bool = False) -> MaterializationResult:
    release_file = path / "release.json"
    ready_file = path / "_READY"
    manifest_file = path / "manifest.jsonl"
    if not release_file.exists() or not ready_file.exists() or not manifest_file.exists():
        raise IntegrityError(f"release is incomplete: {path}")

    result = MaterializationResult(**json.loads(release_file.read_text(encoding="utf-8")))
    manifest_digest = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    if manifest_digest != result.manifest_sha256:
        raise IntegrityError("materialized manifest checksum does not match release.json")
    ready = json.loads(ready_file.read_text(encoding="utf-8"))
    if ready.get("lakefs_commit") != result.lakefs_commit:
        raise IntegrityError("_READY commit does not match release.json")
    if ready.get("manifest_sha256") != result.manifest_sha256:
        raise IntegrityError("_READY manifest checksum does not match release.json")
    manifest = Manifest.load(manifest_file)
    if len(manifest.entries) != result.file_count:
        raise IntegrityError("manifest file count does not match release.json")
    if deep:
        actual_size = 0
        release_root = path.resolve()
        for entry in manifest.entries:
            materialized = (release_root / entry.target_path).resolve()
            if release_root not in materialized.parents or not materialized.is_file():
                raise IntegrityError(f"materialized file is missing: {entry.target_path}")
            size = materialized.stat().st_size
            actual_size += size
            if entry.size_bytes is not None and size != entry.size_bytes:
                raise IntegrityError(
                    f"{entry.target_path}: expected {entry.size_bytes} bytes, found {size}"
                )
            if entry.sha256 is not None:
                digest = _sha256_file(materialized)
                if digest != entry.sha256:
                    raise IntegrityError(
                        f"{entry.target_path}: expected sha256 {entry.sha256}, found {digest}"
                    )
        if actual_size != result.size_bytes:
            raise IntegrityError("materialized total size does not match release.json")
    return result


def certify_prepared_release(
    *,
    prepared_dir: Path,
    target_root: Path,
    dataset: str,
    repository: str,
    source_reference: str,
    commit_id: str,
    manifest: Manifest,
    lakefs_tag: Optional[str] = None,
    paimon_snapshot_id: Optional[str] = None,
) -> MaterializationResult:
    """Validate an already prepared CPFS directory and atomically publish it without copying."""
    _validate_component(dataset, "dataset")
    _validate_component(commit_id, "commit_id")
    prepared = prepared_dir.resolve()
    root = target_root.resolve()
    if not prepared.is_dir() or prepared.is_symlink():
        raise ValueError("prepared_dir must be an existing real directory")
    if prepared == root or prepared == Path(prepared.anchor):
        raise ValueError("prepared_dir cannot be the target root or filesystem root")
    if prepared.stat().st_dev != root.stat().st_dev:
        raise ValueError("prepared_dir and target_root must be on the same filesystem for atomic publish")

    release_dir = root / dataset / commit_id
    lock_dir = root / ".locks" / dataset
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{commit_id}.lock"

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        existing = Materializer._load_existing(release_dir, manifest.sha256)
        if existing is not None:
            return existing

        expected_paths = {entry.target_path for entry in manifest.entries}
        actual_paths = {
            path.relative_to(prepared).as_posix()
            for path in prepared.rglob("*")
            if path.is_file()
        }
        if actual_paths != expected_paths:
            missing = sorted(expected_paths - actual_paths)
            unexpected = sorted(actual_paths - expected_paths)
            raise IntegrityError(
                f"prepared directory does not match manifest; missing={missing[:10]}, "
                f"unexpected={unexpected[:10]}"
            )

        size_bytes = 0
        for entry in manifest.entries:
            path = (prepared / entry.target_path).resolve()
            if prepared not in path.parents or path.is_symlink() or not path.is_file():
                raise IntegrityError(f"invalid prepared file: {entry.target_path}")
            size = path.stat().st_size
            size_bytes += size
            if entry.size_bytes is not None and size != entry.size_bytes:
                raise IntegrityError(
                    f"{entry.target_path}: expected {entry.size_bytes} bytes, found {size}"
                )
            if entry.sha256 is not None:
                digest = _sha256_file(path)
                if digest != entry.sha256:
                    raise IntegrityError(
                        f"{entry.target_path}: expected sha256 {entry.sha256}, found {digest}"
                    )

        result = MaterializationResult(
            dataset=dataset,
            repository=repository,
            source_reference=source_reference,
            lakefs_commit=commit_id,
            lakefs_tag=lakefs_tag,
            paimon_snapshot_id=paimon_snapshot_id,
            manifest_sha256=manifest.sha256,
            file_count=len(manifest.entries),
            size_bytes=size_bytes,
            release_path=str(release_dir),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        metadata_paths = [prepared / "manifest.jsonl", prepared / "release.json"]
        try:
            metadata_paths[0].write_bytes(manifest.raw_bytes)
            _write_json(metadata_paths[1], asdict(result))
            release_dir.parent.mkdir(parents=True, exist_ok=True)
            if release_dir.exists():
                raise ReleaseConflictError(f"release appeared concurrently: {release_dir}")
            os.rename(prepared, release_dir)
            _write_json_atomic(
                release_dir / "_READY",
                {
                    "lakefs_commit": commit_id,
                    "manifest_sha256": manifest.sha256,
                    "status": "READY",
                },
            )
            return result
        except Exception:
            if prepared.exists():
                for path in metadata_paths:
                    if path.exists():
                        path.unlink()
            raise


def _validate_component(value: str, name: str) -> None:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{name} must be a non-empty single path component")


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    _write_json(temporary, value)
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(8 * 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
