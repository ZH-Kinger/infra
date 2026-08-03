from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
from typing import BinaryIO, ContextManager, Iterator, Protocol

from .errors import OptionalDependencyError
from .manifest import ManifestEntry


class SourceReader(Protocol):
    def open(self, commit_id: str, entry: ManifestEntry) -> ContextManager[BinaryIO]: ...


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
    """Read immutable objects through the lakeFS S3 Gateway.

    `path_prefix` 是 manifest 的 `source_key` 与 **Commit 内实际路径** 之间的差值。

    为什么需要它：`source_key` 的含义是「在来源自己的坐标系里怎么找到这个文件」，
    而不同命令的来源不同——

        archive       来源是 CPFS staging 目录  → source_key 是 staging 内相对路径
        materialize   来源是 lakeFS Commit      → 需要 Commit 内路径

    `commit --destination D` 会把对象放到 Commit 的 `D/` 下面，所以同一份 manifest
    拿去 materialize 时，键要变成 `<commit>/D/<source_key>`。少了 D 就是全量 404，
    而且要等到逐个 get_object 才暴露——那时 Commit 和 Tag 都已经建好了。

    `scan-oss` 产出的 manifest 已经把 D 写进 source_key 了，所以那条路径
    `path_prefix` 留空；`scan` 产出的没有，必须显式传。
    """

    def __init__(
        self,
        repository: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
        verify_tls: bool = True,
        path_prefix: str = "",
    ) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise OptionalDependencyError(
                "lakeFS S3 support requires `pip install -e '.[s3]'`"
            ) from exc

        self.repository = repository
        self.path_prefix = path_prefix.strip("/")
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
        parts = [commit_id]
        if self.path_prefix:
            parts.append(self.path_prefix)
        parts.append(entry.source_key)
        key = "/".join(parts)
        response = self.client.get_object(Bucket=self.repository, Key=key)
        with closing(response["Body"]) as stream:
            yield stream
