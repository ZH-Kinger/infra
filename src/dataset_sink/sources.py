from __future__ import annotations

from contextlib import contextmanager, closing
from pathlib import Path
from typing import BinaryIO, ContextManager, Iterator, Protocol

from .errors import OptionalDependencyError
from .manifest import ManifestEntry


class SourceReader(Protocol):
    def open(self, commit_id: str, entry: ManifestEntry) -> ContextManager[BinaryIO]:
        ...


class LocalSourceReader:
    """Local adapter used for development and tests."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @contextmanager
    def open(self, commit_id: str, entry: ManifestEntry) -> Iterator[BinaryIO]:
        del commit_id
        path = (self.root / entry.source_key).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError(f"source path escapes local root: {entry.source_key}")
        with path.open("rb") as stream:
            yield stream


class LakeFSS3SourceReader:
    """Read immutable objects through the lakeFS S3 Gateway."""

    def __init__(
        self,
        repository: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
        verify_tls: bool = True,
    ) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise OptionalDependencyError(
                "lakeFS S3 support requires `pip install -e '.[s3]'`"
            ) from exc

        self.repository = repository
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            verify=verify_tls,
            config=Config(s3={"addressing_style": "path"}),
        )

    @contextmanager
    def open(self, commit_id: str, entry: ManifestEntry) -> Iterator[BinaryIO]:
        key = f"{commit_id}/{entry.source_key}"
        response = self.client.get_object(Bucket=self.repository, Key=key)
        with closing(response["Body"]) as stream:
            yield stream
