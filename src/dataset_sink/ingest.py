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
    with path.open("rb") as stream:
        return _digest_stream(stream)


def _digest_stream(stream: BinaryIO) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


# ---------------------------------------------------------------------------
# ①' scan-object-store：把**已经在对象存储里**的存量数据变成 manifest
#
# 存量数据不需要 archive——字节已经躺在持久位置上了。而 lakeFS import 是零拷贝的，
# 所以「存量 OSS 数据 → Commit」这条路径**全程不搬一个字节**，建 Commit 是秒级。
#
# 代价在完整性上：对象存储只会告诉你 size 和 ETag（ETag 是 MD5，且分片上传时
# 连 MD5 都不是），拿不到 SHA-256。要得到和 cpfs-ingest 同等强度的保证，必须
# 完整读一遍数据来算——这就是 with_digest 的含义。默认开启，因为一个没有
# SHA-256 的 release 会让 verify --deep 和 training-guard --deep 永久退化成
# 只比大小，而这个损失是不可逆的（manifest 随发布固化，事后补不上）。
# ---------------------------------------------------------------------------


class ObjectReader(Protocol):
    """只读对象存储的抽象。有本地实现，因此单元测试不需要网络和云凭证。"""

    def list_objects(self, prefix: str) -> Iterable[Tuple[str, int]]: ...

    def open(self, key: str) -> BinaryIO: ...


class LocalObjectReader:
    """本地目录充当对象存储，用于开发与测试。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def list_objects(self, prefix: str) -> Iterable[Tuple[str, int]]:
        base = self.root / prefix if prefix else self.root
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink():
                yield path.relative_to(self.root).as_posix(), path.stat().st_size

    def open(self, key: str) -> BinaryIO:
        return (self.root / key).open("rb")


class OssObjectReader:
    """通过 S3 兼容接口读取阿里云 OSS，和 OssObjectWriter 共用同一套依赖。"""

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        security_token: Optional[str] = None,
        region: str = "oss",
        verify_tls: bool = True,
    ) -> None:
        self.bucket = bucket
        self.client = _s3_client(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            security_token=security_token,
            region=region,
            verify_tls=verify_tls,
        )

    def list_objects(self, prefix: str) -> Iterable[Tuple[str, int]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                yield obj["Key"], int(obj.get("Size", 0))

    def open(self, key: str) -> BinaryIO:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]


@dataclass(frozen=True)
class ObjectScanResult:
    entries: Tuple[ManifestEntry, ...]
    file_count: int
    total_bytes: int
    digested: bool


def scan_object_store(
    reader: ObjectReader,
    prefix: str,
    destination: str,
    *,
    with_digest: bool = True,
    workers: int = 8,
) -> ObjectScanResult:
    """列举对象存储前缀下的存量数据，生成 manifest。

    产出的 manifest 和 lakeFS import 的输入来自**同一次列举**，所以
    「manifest 描述的内容」和「Commit 实际包含的内容」按构造就是一致的——
    这一点比 cpfs-ingest 那条路更强，那边两者是分别确定的。

    `destination` 必须和后面 `commit --destination` 用的是同一个值。原因是
    这两个字段指的是不同坐标系，混淆会让 materialize 全量 404：

        target_path  release 内的相对路径          shards/a.bin
        source_key   **Commit 内**的路径           datasets/robotics/shards/a.bin

    import 会把 `prefix` 下的对象放到 Commit 的 `destination` 下面，而
    materialize 从 lakeFS S3 Gateway 读取时用的键是 `<commit>/<source_key>`。
    所以 source_key 必须带上 destination 前缀。cpfs-ingest 那条路径不会踩到
    这个坑，因为它用 certify（只看 target_path，不回读 lakeFS）。

    `with_digest=False` 时只记录 size，不读取任何数据。快，但发布出来的
    release 永久失去 SHA-256 校验能力，只适合先摸清前缀里有什么。
    """
    normalized = _normalize_relative(prefix)
    if not normalized:
        raise DatasetSinkError("prefix 不能为空：必须指向对象存储里一个明确的前缀")
    destination_prefix = validate_destination(destination)

    listing: List[Tuple[str, str, int]] = []  # (key, relative, size)
    seen: Dict[str, str] = {}
    for key, size in reader.list_objects(normalized + "/"):
        if key.endswith("/"):
            # 控制台创建的「目录」是零字节的伪对象，不是数据。
            continue
        relative = key[len(normalized) + 1 :] if key.startswith(normalized + "/") else None
        if not relative:
            continue
        _validate_object_relative_path(key, relative)
        if relative in seen:
            raise DatasetSinkError(
                f"两个对象键规范化后指向同一个路径: {seen[relative]!r} 与 {key!r}"
            )
        seen[relative] = key
        listing.append((key, relative, size))

    if not listing:
        raise DatasetSinkError(
            f"前缀下没有对象: {normalized}。请确认桶名、前缀和当前身份的读权限。"
        )

    if with_digest:
        digests: Dict[str, Tuple[int, str]] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(_digest_object, reader, key): relative
                for key, relative, _ in listing
            }
            for future in as_completed(futures):
                digests[futures[future]] = future.result()

        for _, relative, size in listing:
            actual = digests[relative][0]
            if actual != size:
                raise IntegrityError(
                    f"{relative}: 列举时报告 {size} 字节，读取时得到 {actual} 字节。"
                    "对象在扫描期间被改动过——存量前缀在建 Commit 前必须先冻结写入。"
                )

        entries = tuple(
            ManifestEntry(
                source_key=f"{destination_prefix}/{relative}",
                target_path=relative,
                size_bytes=digests[relative][0],
                sha256=digests[relative][1],
            )
            for _, relative, _ in sorted(listing, key=lambda item: item[1])
        )
    else:
        entries = tuple(
            ManifestEntry(
                source_key=f"{destination_prefix}/{relative}",
                target_path=relative,
                size_bytes=size,
            )
            for _, relative, size in sorted(listing, key=lambda item: item[1])
        )

    return ObjectScanResult(
        entries=entries,
        file_count=len(entries),
        total_bytes=sum(e.size_bytes or 0 for e in entries),
        digested=with_digest,
    )


def _digest_object(reader: ObjectReader, key: str) -> Tuple[int, str]:
    stream = reader.open(key)
    try:
        return _digest_stream(stream)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()


def _validate_object_relative_path(key: str, relative: str) -> None:
    """对象键的命名空间比文件系统宽松得多，进 manifest 前必须收紧。

    OSS 允许 `a//b`、`a/./b`、`a/../b` 这类键共存且互不相同，而它们落到
    文件系统上会塌缩成同一个路径。不拦住的话，materialize 时后写的文件会
    覆盖先写的，release 里少了文件但 file_count 仍然对得上。
    """
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise DatasetSinkError(
            f"对象键含有无法安全落到文件系统的路径段: {key!r}。"
            "请先在对象存储侧整理这些键，或换一个更精确的前缀。"
        )
    if relative != path.as_posix():
        raise DatasetSinkError(f"对象键规范化前后不一致（可能含空段 `//`）: {key!r}。")


# ---------------------------------------------------------------------------
# ② archive：把 staging 归档到对象存储
# ---------------------------------------------------------------------------


def _s3_client(
    *,
    endpoint_url: str,
    access_key_id: Optional[str],
    secret_access_key: Optional[str],
    security_token: Optional[str],
    region: str,
    verify_tls: bool,
):
    """建一个指向 OSS 的 S3 兼容客户端。

    `security_token` 走 boto3 的 `aws_session_token` 参数。之前是建好客户端后
    去改 `client._request_signer._credentials.token`——那是私有属性，boto3
    一次升级就可能失效，而失效的表现是「凭证静默降级为无 token」，报错发生在
    很远的地方。
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise OptionalDependencyError("访问 OSS 需要 `pip install -e '.[s3]'`") from exc

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=security_token,
        region_name=region,
        verify=verify_tls,
        config=Config(s3={"addressing_style": "virtual"}),
    )


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
        security_token: Optional[str] = None,
        region: str = "oss",
        verify_tls: bool = True,
    ) -> None:
        self.bucket = bucket
        self.client = _s3_client(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            security_token=security_token,
            region=region,
            verify_tls=verify_tls,
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


def assert_manifest_matches_destination(manifest: Manifest, destination: str) -> None:
    """确认 manifest 的 source_key 确实指向 Commit 内 `destination` 下的路径。

    这是 scan-oss 与 commit 之间唯一的隐式契约：两条命令各自接收 destination，
    填错一个不会立刻报错，而是等到 materialize 从 lakeFS 逐个 get_object 时
    全量 404——那时数据已经 import 完、Commit 和 Tag 都建好了，回滚很难看。
    这里花一次 O(n) 的字符串比较把它挡在建 Commit 之前。

    只对 scan-oss 产出的 manifest 有意义。cpfs-ingest 的 manifest 里
    source_key 是 staging 内的相对路径（archive 用它读本地文件），
    与 Commit 内路径无关，所以那条路径不做这个校验。
    """
    normalized = validate_destination(destination)
    prefix = normalized + "/"
    mismatched = [e.source_key for e in manifest.entries if not e.source_key.startswith(prefix)]
    if mismatched:
        shown = ", ".join(sorted(mismatched)[:5])
        raise DatasetSinkError(
            f"manifest 的 source_key 不在 destination {normalized!r} 下面: {shown}"
            f"（共 {len(mismatched)} 条）。\n"
            "scan-oss 与 commit 的 --destination 必须填同一个值，否则 import 之后 "
            "materialize 会在 lakeFS 里找不到任何对象。"
        )


def summarize_entries(entries: Sequence[ManifestEntry]) -> Dict[str, object]:
    """给 CLI 输出用的简要统计。"""
    sizes: Iterable[int] = [e.size_bytes or 0 for e in entries]
    total = sum(sizes)
    return {
        "file_count": len(entries),
        "total_bytes": total,
        "total_gib": round(total / (1024**3), 3),
    }
