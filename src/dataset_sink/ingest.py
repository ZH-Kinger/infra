"""把 CPFS 上处理完的数据接入版本体系：扫描 → 归档 → 产生 lakeFS Commit。

这三步补齐的是 `certify` 的前置条件。`certify --commit` 要求 Commit 已经存在，
而用户在 CPFS 上预处理出来的新数据并没有 Commit——本模块负责造出它。

为什么不能跳过「归档到对象存储」直接在 CPFS 上发布：
CPFS 是热存储不是归档层（容量有限、按容量计费、通常无跨区冗余），而 lakeFS 的
Commit 必须指向持久的字节位置。如果 Commit 指向 CPFS，一旦 release 目录被淘汰
就会变成悬空引用——版本记录还在，数据没了。

所以正确的分工是：
    对象存储 = 冷归档 + 版本真相的物理载体（可长期保留，可转低频存储）
    CPFS     = 为训练速度而存在的热副本（可随时淘汰，需要时重新沉降回来）

归档之后用 lakeFS 的 import 建立 Commit——import 只读对象元数据，**不搬运数据**，
所以整条链路里字节只被真正搬了一次（CPFS → 对象存储）。
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from .errors import DatasetSinkError, IntegrityError, OptionalDependencyError
from .manifest import Manifest, ManifestEntry

_READ_CHUNK = 8 * 1024 * 1024


def _normalize_relative(value: str) -> str:
    """去掉首尾空白与斜杠。

    必须先去空白再去斜杠、然后再去一次空白："  /  ".strip("/") 只会去掉
    首尾斜杠而留下空格，结果非空，空值检查就会被绕过。
    """
    return value.strip().strip("/").strip()


# 发布协议自己的产物，不属于数据集内容，扫描时必须排除，
# 否则它们会被当成数据归档进对象存储并进入 Commit。
_PROTOCOL_FILES = frozenset({"_READY", "release.json", "manifest.jsonl"})


# ---------------------------------------------------------------------------
# ① scan：把一个 CPFS staging 目录变成 manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanResult:
    entries: Tuple[ManifestEntry, ...]
    file_count: int
    total_bytes: int


def scan_staging(
    staging_dir: Path,
    workers: int = 8,
) -> ScanResult:
    """遍历 staging 目录，算出每个文件的大小与 SHA-256。

    staging 目录的内部布局**就是** release 里的布局：文件的相对路径直接作为
    `target_path`。这也是 `certify` 能做到零拷贝发布的前提。

    **staging 里必须只有数据集内容。** 任何多余文件都会让 scan 失败。

    这一点不能放松：`certify` 在发布前会要求目录内容与 manifest 逐个对齐，
    多一个文件就拒绝发布。如果 scan 在这里悄悄跳过 `.DS_Store` 之类的东西，
    使用者会在三步之后才撞上 certify 的报错，而那时已经白白归档了一遍数据。
    两个命令对「什么算数据集内容」必须是同一个定义，且尽早失败。
    """
    root = staging_dir.resolve()
    if not root.is_dir():
        raise DatasetSinkError(f"staging 目录不存在或不是目录: {staging_dir}")

    files: List[Path] = []
    extras: List[str] = []

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            # 符号链接既无法归档进对象存储，也可能指向 staging 之外。
            raise DatasetSinkError(
                f"staging 里存在符号链接，无法归档: {relative.as_posix()}。"
                "请改成真实文件，或从 staging 中移除。"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise DatasetSinkError(f"不支持的文件类型: {relative.as_posix()}")

        if any(part.startswith(".") for part in relative.parts):
            extras.append(relative.as_posix())
        elif path.name in _PROTOCOL_FILES:
            # 出现这些说明 staging 其实是个已发布的 release，或者上一次发布
            # 失败留下的残骸——两种情况都该停下来看看，而不是继续。
            extras.append(relative.as_posix())
        else:
            files.append(path)

    if extras:
        shown = ", ".join(extras[:10])
        more = f"（共 {len(extras)} 个）" if len(extras) > 10 else ""
        raise DatasetSinkError(
            f"staging 里有不属于数据集内容的文件{more}: {shown}。\n"
            "certify 发布时会因为目录与 manifest 不一致而拒绝，所以这里先拦下。\n"
            f"清理办法：find {staging_dir} \\( -name '.*' -o -name '_READY' "
            "-o -name 'release.json' -o -name 'manifest.jsonl' \\) -delete\n"
            "如果这个目录本来就是一个已发布的 release，那它不该作为 staging 使用。"
        )

    if not files:
        raise DatasetSinkError(f"staging 目录里没有可归档的文件: {staging_dir}")

    digests: Dict[str, Tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(_size_and_digest, path): path for path in files}
        for future in as_completed(futures):
            path = futures[future]
            relative = path.relative_to(root).as_posix()
            digests[relative] = future.result()

    entries = tuple(
        ManifestEntry(
            # staging 已按 release 布局组织，两者相同；归档到对象存储时
            # source_key 也就是对象键的相对部分。
            source_key=relative,
            target_path=relative,
            size_bytes=digests[relative][0],
            sha256=digests[relative][1],
        )
        for relative in sorted(digests)
    )

    return ScanResult(
        entries=entries,
        file_count=len(entries),
        total_bytes=sum(e.size_bytes or 0 for e in entries),
    )


def _size_and_digest(path: Path) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


# ---------------------------------------------------------------------------
# ② archive：把 staging 归档到对象存储
# ---------------------------------------------------------------------------


class ObjectWriter(Protocol):
    """归档目标的抽象。有本地实现，因此单元测试不需要网络和云凭证。"""

    def exists(self, key: str, size_bytes: int) -> bool: ...

    def put(self, key: str, stream: BinaryIO) -> None: ...


class LocalObjectWriter:
    """本地目录充当对象存储，用于开发与测试。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def exists(self, key: str, size_bytes: int) -> bool:
        path = self.root / key
        return path.is_file() and path.stat().st_size == size_bytes

    def put(self, key: str, stream: BinaryIO) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as out:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(temporary, path)


class OssObjectWriter:
    """通过 S3 兼容接口写入阿里云 OSS。

    用 S3 兼容而不是 OSS 原生 SDK，是为了和 `sources.py` 里读 lakeFS S3 Gateway
    共用同一套依赖（boto3 在 optional extras 里），核心逻辑保持零运行时依赖。
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        region: str = "oss",
        verify_tls: bool = True,
    ) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise OptionalDependencyError("归档到 OSS 需要 `pip install -e '.[s3]'`") from exc

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            verify=verify_tls,
            config=Config(s3={"addressing_style": "virtual"}),
        )

    def exists(self, key: str, size_bytes: int) -> bool:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001 - 任何查不到都按「不存在」处理，重传即可
            return False
        return int(head.get("ContentLength", -1)) == size_bytes

    def put(self, key: str, stream: BinaryIO) -> None:
        self.client.upload_fileobj(stream, self.bucket, key)


@dataclass(frozen=True)
class ArchiveResult:
    uploaded: int
    skipped_existing: int
    total_bytes: int
    prefix: str


class _HashingReader:
    """边被读取边算 SHA-256，让归档只读一遍文件。

    TB 级数据集下，「先算哈希再上传」意味着两遍完整 IO。包一层之后
    上传器读多少就算多少，校验不额外付出代价。
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk:
            self.size += len(chunk)
            self._digest.update(chunk)
        return chunk

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def archive_staging(
    staging_dir: Path,
    manifest: Manifest,
    writer: ObjectWriter,
    prefix: str,
    workers: int = 8,
) -> ArchiveResult:
    """把 staging 里的文件归档到对象存储的 `prefix` 下。

    幂等：目标对象已存在且大小一致就跳过，所以中断后重跑只补缺口，
    不会把已经传完的 TB 数据重传一遍。
    """
    root = staging_dir.resolve()
    normalized = _normalize_relative(prefix)
    if not normalized:
        raise DatasetSinkError("prefix 不能为空：归档必须落在一个明确的前缀下")

    def _one(entry: ManifestEntry) -> Tuple[bool, int]:
        source = (root / entry.source_key).resolve()
        if root != source and root not in source.parents:
            raise DatasetSinkError(f"路径逃逸出 staging 根目录: {entry.source_key}")
        if not source.is_file():
            raise DatasetSinkError(f"manifest 里的文件在 staging 中不存在: {entry.source_key}")

        size = entry.size_bytes if entry.size_bytes is not None else source.stat().st_size
        key = f"{normalized}/{entry.target_path}"

        if writer.exists(key, size):
            return False, size

        with source.open("rb") as raw:
            reader = _HashingReader(raw)
            writer.put(key, reader)  # type: ignore[arg-type]

        if entry.sha256 is not None and reader.hexdigest() != entry.sha256:
            raise IntegrityError(
                f"{entry.target_path}: manifest 声明 sha256 {entry.sha256}，"
                f"归档时实际读到 {reader.hexdigest()}。staging 在扫描后被改动过。"
            )
        if entry.size_bytes is not None and reader.size != entry.size_bytes:
            raise IntegrityError(
                f"{entry.target_path}: manifest 声明 {entry.size_bytes} 字节，"
                f"归档时实际读到 {reader.size} 字节。"
            )
        return True, reader.size

    uploaded = 0
    skipped = 0
    total = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(_one, entry) for entry in manifest.entries]
        for future in as_completed(futures):
            did_upload, size = future.result()
            total += size
            if did_upload:
                uploaded += 1
            else:
                skipped += 1

    return ArchiveResult(
        uploaded=uploaded,
        skipped_existing=skipped,
        total_bytes=total,
        prefix=normalized,
    )


# ---------------------------------------------------------------------------
# ③ commit：用 lakeFS import 从归档产生 Commit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitResult:
    commit_id: str
    ingested_objects: Optional[int]
    branch: str
    tag: Optional[str]
    object_store_uri: str


class LakeFSImporter(Protocol):
    def import_prefix(
        self,
        *,
        repository: str,
        branch: str,
        object_store_uri: str,
        destination: str,
        message: str,
        metadata: Dict[str, str],
        tag: Optional[str],
    ) -> CommitResult: ...


class SdkLakeFSImporter:
    """基于 lakeFS 官方 Python SDK 的实现。

    用的是零拷贝 import：lakeFS 只读对象存储里的对象元数据来建立 Commit，
    **不复制数据**。所以 TB 级数据集建 Commit 是秒级到分钟级，取决于对象个数
    而不是字节数。
    """

    def __init__(
        self,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        try:
            import lakefs
            from lakefs.client import Client
        except ImportError as exc:
            raise OptionalDependencyError(
                "lakeFS import 需要 `pip install -e '.[lakefs]'`"
            ) from exc

        self._lakefs = lakefs
        self._client = Client(
            host=endpoint,
            username=access_key_id,
            password=secret_access_key,
        )

    def import_prefix(
        self,
        *,
        repository: str,
        branch: str,
        object_store_uri: str,
        destination: str,
        message: str,
        metadata: Dict[str, str],
        tag: Optional[str],
    ) -> CommitResult:
        repo = self._lakefs.Repository(repository, client=self._client)
        target = repo.branch(branch)

        manager = target.import_data(commit_message=message, metadata=dict(metadata))
        manager.prefix(object_store_uri=object_store_uri, destination=destination)
        status = manager.run()

        if getattr(status, "error", None) is not None:
            raise DatasetSinkError(f"lakeFS import 失败: {status.error.message}")
        if not getattr(status, "completed", False):
            raise DatasetSinkError("lakeFS import 未完成就返回了，请检查 lakeFS 服务端状态")

        commit = getattr(status, "commit", None)
        commit_id = getattr(commit, "id", None) if commit is not None else None
        if not commit_id:
            raise DatasetSinkError("lakeFS import 完成但没有返回 Commit ID")

        created_tag = None
        if tag:
            # exist_ok=False：同名 Tag 已存在就报错。Tag 指向不可变 Commit，
            # 静默覆盖等于让「同一个 Tag 指向不同数据」，破坏可复现性。
            repo.tag(tag).create(source_ref=str(commit_id), exist_ok=False)
            created_tag = tag

        return CommitResult(
            commit_id=str(commit_id),
            ingested_objects=getattr(status, "ingested_objects", None),
            branch=branch,
            tag=created_tag,
            object_store_uri=object_store_uri,
        )


def import_and_commit(
    *,
    repository: str,
    branch: str,
    object_store_uri: str,
    destination: str,
    message: str,
    metadata: Optional[Dict[str, str]] = None,
    tag: Optional[str] = None,
    importer: Optional[LakeFSImporter] = None,
    endpoint: Optional[str] = None,
    access_key_id: Optional[str] = None,
    secret_access_key: Optional[str] = None,
) -> CommitResult:
    """从对象存储前缀零拷贝导入 lakeFS 并产生 Commit。

    `importer` 参数存在是为了单元测试能在没有 lakeFS 服务的情况下验证编排逻辑。
    """
    if importer is None:
        if not (endpoint and access_key_id and secret_access_key):
            raise ValueError("未提供 importer 时，必须给出 lakeFS endpoint 与凭证")
        importer = SdkLakeFSImporter(endpoint, access_key_id, secret_access_key)

    return importer.import_prefix(
        repository=repository,
        branch=branch,
        object_store_uri=object_store_uri,
        destination=_normalize_relative(destination),
        message=message,
        metadata=dict(metadata or {}),
        tag=tag,
    )


def build_commit_metadata(
    *,
    manifest: Manifest,
    scan: Optional[ScanResult] = None,
    paimon_snapshot_id: Optional[str] = None,
    extra: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """构造附在 Commit 上的元数据。

    `manifest_sha256` 是整条链路的关联键：它同时出现在 lakeFS Commit、
    CPFS 的 release.json、PAI Dataset Version 的 Label 和训练容器的环境变量里。
    四处独立记录、交叉校验，任何一处对不上，training-guard 都会拦下来。
    """
    metadata: Dict[str, str] = {
        "manifest_sha256": manifest.sha256,
        "file_count": str(len(manifest.entries)),
        "source": "dataset-sink",
    }
    if scan is not None:
        metadata["total_bytes"] = str(scan.total_bytes)
    elif manifest.declared_size_bytes is not None:
        metadata["total_bytes"] = str(manifest.declared_size_bytes)
    if paimon_snapshot_id:
        metadata["paimon_snapshot_id"] = paimon_snapshot_id
    if extra:
        metadata.update({str(k): str(v) for k, v in extra.items()})
    return metadata


def object_store_uri_for(scheme_uri: str, prefix: str) -> str:
    """把桶级 URI 与前缀拼成 lakeFS import 要的对象存储 URI。

    lakeFS 要求 URI 指向「前缀」，末尾的斜杠有意义——没有它，lakeFS 可能把
    它当成单个对象而不是前缀，导入结果就只有一个对象。
    """
    base = scheme_uri.rstrip("/")
    normalized = _normalize_relative(prefix)
    if not normalized:
        raise DatasetSinkError("prefix 不能为空")
    return f"{base}/{normalized}/"


def validate_destination(destination: str) -> str:
    """校验并**规范化** Commit 内的目标路径，拒绝 `..` 与空值。

    返回的是 PurePosixPath 折叠后的形式，不是原始字符串。两者必须一致：
    如果用折叠后的形式做安全检查、却返回原始字符串，`./x` 这类输入会通过
    检查然后在 lakeFS 里建出字面量 `./x` 路径。
    """
    normalized = _normalize_relative(destination)
    if not normalized:
        raise DatasetSinkError("destination 不能为空")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise DatasetSinkError(f"destination 必须是安全的相对路径: {destination!r}")
    collapsed = path.as_posix()
    if collapsed in ("", "."):
        raise DatasetSinkError(f"destination 规范化后为空: {destination!r}")
    return collapsed


def summarize_entries(entries: Sequence[ManifestEntry]) -> Dict[str, object]:
    """给 CLI 输出用的简要统计。"""
    sizes: Iterable[int] = [e.size_bytes or 0 for e in entries]
    total = sum(sizes)
    return {
        "file_count": len(entries),
        "total_bytes": total,
        "total_gib": round(total / (1024**3), 3),
    }
