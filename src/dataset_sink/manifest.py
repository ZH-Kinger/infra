from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Tuple

from .errors import ManifestError


@dataclass(frozen=True)
class ManifestEntry:
    source_key: str
    target_path: str
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None

    @classmethod
    def from_dict(cls, value: dict, line_number: int) -> "ManifestEntry":
        source_key = value.get("source_key")
        target_path = value.get("target_path")
        if not isinstance(source_key, str) or not source_key.strip():
            raise ManifestError(f"line {line_number}: source_key must be a non-empty string")
        if not isinstance(target_path, str) or not target_path.strip():
            raise ManifestError(f"line {line_number}: target_path must be a non-empty string")

        _validate_relative_path(source_key, "source_key", line_number)
        _validate_relative_path(target_path, "target_path", line_number)

        size_bytes = value.get("size_bytes")
        if size_bytes is not None and (not isinstance(size_bytes, int) or size_bytes < 0):
            raise ManifestError(f"line {line_number}: size_bytes must be a non-negative integer")

        digest = value.get("sha256")
        if digest is not None:
            if not isinstance(digest, str) or len(digest) != 64:
                raise ManifestError(f"line {line_number}: sha256 must contain 64 hexadecimal characters")
            try:
                int(digest, 16)
            except ValueError as exc:
                raise ManifestError(f"line {line_number}: sha256 is not hexadecimal") from exc
            digest = digest.lower()

        return cls(
            source_key=source_key,
            target_path=target_path,
            size_bytes=size_bytes,
            sha256=digest,
        )


@dataclass(frozen=True)
class Manifest:
    entries: Tuple[ManifestEntry, ...]
    sha256: str
    raw_bytes: bytes

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        raw = path.read_bytes()
        entries = []
        targets = set()

        for line_number, raw_line in enumerate(raw.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ManifestError(f"line {line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ManifestError(f"line {line_number}: each line must be a JSON object")
            entry = ManifestEntry.from_dict(value, line_number)
            if entry.target_path in targets:
                raise ManifestError(f"line {line_number}: duplicate target_path {entry.target_path!r}")
            targets.add(entry.target_path)
            entries.append(entry)

        if not entries:
            raise ManifestError("manifest contains no entries")

        return cls(
            entries=tuple(entries),
            sha256=hashlib.sha256(raw).hexdigest(),
            raw_bytes=raw,
        )

    @property
    def declared_size_bytes(self) -> Optional[int]:
        sizes = [entry.size_bytes for entry in self.entries]
        if any(size is None for size in sizes):
            return None
        return sum(size for size in sizes if size is not None)


def _validate_relative_path(value: str, field: str, line_number: int) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ManifestError(f"line {line_number}: {field} must be a safe relative POSIX path")
    if "\\" in value:
        raise ManifestError(f"line {line_number}: {field} must use '/' separators")


def dump_manifest(entries: Iterable[ManifestEntry], path: Path) -> None:
    """Write deterministic JSONL, useful for index-export jobs and tests."""
    lines = []
    for entry in entries:
        value = {
            "source_key": entry.source_key,
            "target_path": entry.target_path,
        }
        if entry.size_bytes is not None:
            value["size_bytes"] = entry.size_bytes
        if entry.sha256 is not None:
            value["sha256"] = entry.sha256
        lines.append(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

