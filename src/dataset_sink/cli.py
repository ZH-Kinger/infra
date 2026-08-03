from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .aliyun_cli import register_pai_dataset_version
from .dataflow import CpfsDataFlow
from .errors import DatasetSinkError
from .ingest import (
    LocalObjectReader,
    LocalObjectWriter,
    OssObjectReader,
    OssObjectWriter,
    archive_staging,
    assert_manifest_matches_destination,
    build_commit_metadata,
    import_and_commit,
    object_store_uri_for,
    scan_object_store,
    scan_staging,
    summarize_entries,
    validate_destination,
)
from .lakefs_refs import resolve_reference
from .manifest import Manifest, dump_manifest
from .materializer import Materializer, certify_prepared_release, verify_release
from .pai import CpfsRegistration, build_create_dataset_version_request
from .reclaim import (
    AssumeRecoverable,
    CpfsEvictStrategy,
    HardDeleteStrategy,
    LakeFSCommitProbe,
    execute_plan,
    plan_reclaim,
    scan_releases,
    sweep_trash,
)
from .registry import load_registry
from .sources import LakeFSS3SourceReader, LocalSourceReader
from .training_guard import validate_training_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataset-sink",
        description="Materialize immutable lakeFS commits onto CPFS for PAI training",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    materialize = commands.add_parser("materialize", help="publish an immutable CPFS release")
    materialize.add_argument("--dataset", required=True)
    materialize.add_argument("--repository", required=True)
    ref_group = materialize.add_mutually_exclusive_group(required=True)
    ref_group.add_argument("--commit", help="already-resolved immutable lakeFS commit")
    ref_group.add_argument("--ref", help="lakeFS tag/branch/ref expression to resolve")
    materialize.add_argument("--manifest", type=Path, required=True)
    materialize.add_argument("--target-root", type=Path, required=True)
    materialize.add_argument("--paimon-snapshot-id")
    materialize.add_argument("--lakefs-tag")
    materialize.add_argument("--workers", type=int, default=8)
    materialize.add_argument("--source", choices=("local", "lakefs-s3"), default="local")
    materialize.add_argument("--local-source-root", type=Path)
    materialize.add_argument("--lakefs-api-endpoint")
    materialize.add_argument("--lakefs-s3-endpoint")
    materialize.add_argument("--lakefs-access-key-id")
    materialize.add_argument("--lakefs-secret-access-key")
    materialize.add_argument("--lakefs-region", default="us-east-1")
    materialize.add_argument(
        "--commit-prefix",
        default="",
        help=(
            "manifest 的 source_key 与 Commit 内路径之间的差值，通常就是 "
            "commit --destination 填的值。scan 产出的 manifest 里 source_key 是 "
            "staging 内相对路径，不含它，必须在这里补上，否则读 lakeFS 会全量 404；"
            "scan-oss 产出的已经含了，留空即可"
        ),
    )
    materialize.add_argument("--no-verify-tls", action="store_true")
    materialize.add_argument(
        "--via",
        choices=("client", "dataflow"),
        default="client",
        help=(
            "client：本进程从 lakeFS S3 Gateway 逐个文件拷贝。"
            "dataflow：交给 CPFS 数据流动做预热（Import），服务端并行、"
            "执行者不需要挂载 CPFS 来搬数据。要求 OSS 前缀布局镜像 CPFS 路径布局"
        ),
    )
    materialize.add_argument("--cpfs-filesystem-id", help="--via dataflow 时必填")
    materialize.add_argument("--cpfs-mount-prefix", help="--via dataflow 时必填")
    materialize.add_argument("--region", help="--via dataflow 时必填")
    materialize.add_argument("--profile", help="aliyun CLI profile")
    materialize.add_argument("--wait-timeout", type=int, default=7200)

    # ---- CPFS 上处理完的数据接入版本体系：scan → archive → commit ----
    #
    # 这三步补齐 certify 的前置条件：certify --commit 要求 Commit 已存在，
    # 而用户在 CPFS 上预处理出来的新数据并没有 Commit。

    scan = commands.add_parser(
        "scan",
        help="扫描 CPFS staging 目录，生成带 size 与 sha256 的 manifest",
    )
    scan.add_argument("staging_dir", type=Path)
    scan.add_argument("--output", type=Path, required=True)
    scan.add_argument("--workers", type=int, default=8)

    scan_oss = commands.add_parser(
        "scan-oss",
        help="扫描对象存储里**已有**的存量数据，生成 manifest（不搬运任何字节）",
    )
    scan_oss.add_argument("--prefix", required=True, help="对象存储里的现有前缀")
    scan_oss.add_argument(
        "--destination",
        required=True,
        help="Commit 内的目标路径，必须与随后 commit --destination 填的值一致",
    )
    scan_oss.add_argument("--output", type=Path, required=True)
    scan_oss.add_argument("--source", choices=("oss", "local"), default="oss")
    scan_oss.add_argument("--bucket", help="OSS 桶名（--source oss 时必填）")
    scan_oss.add_argument("--endpoint-url", help="OSS S3 兼容端点（--source oss 时必填）")
    scan_oss.add_argument("--local-root", type=Path, help="--source local 时的根目录")
    scan_oss.add_argument("--access-key-id", help="留空则回落到环境变量")
    scan_oss.add_argument("--secret-access-key")
    scan_oss.add_argument("--security-token", help="使用 STS 临时凭证时提供")
    scan_oss.add_argument(
        "--no-digest",
        action="store_true",
        help=(
            "只列举 size，不读取对象内容。快，但发布出来的 release 永久失去 "
            "SHA-256 校验能力（manifest 随发布固化，事后补不上）。仅用于摸底。"
        ),
    )
    scan_oss.add_argument("--workers", type=int, default=8)
    scan_oss.add_argument("--no-verify-tls", action="store_true")
    scan_oss.add_argument(
        "--registry",
        type=Path,
        help=(
            "数据源注册表（deploy/data-sources.json）。给了就校验目标位置已注册，"
            "在本地快速失败而不是等 RAM 报 AccessDenied"
        ),
    )

    archive = commands.add_parser(
        "archive",
        help="把 staging 归档到对象存储（冷归档，也是 lakeFS Commit 指向的持久位置）",
    )
    archive.add_argument("staging_dir", type=Path)
    archive.add_argument("--manifest", type=Path, required=True)
    archive.add_argument("--prefix", required=True, help="对象存储内的前缀，如 staging/batch-001")
    archive.add_argument("--target", choices=("oss", "local"), default="oss")
    archive.add_argument("--bucket", help="OSS 桶名（--target oss 时必填）")
    archive.add_argument("--endpoint-url", help="OSS S3 兼容端点（--target oss 时必填）")
    archive.add_argument("--local-root", type=Path, help="--target local 时的落地目录")
    archive.add_argument("--access-key-id", help="留空则回落到环境变量")
    archive.add_argument("--secret-access-key")
    archive.add_argument("--security-token", help="使用 STS 临时凭证时提供")
    archive.add_argument("--workers", type=int, default=8)
    archive.add_argument("--no-verify-tls", action="store_true")
    archive.add_argument(
        "--registry",
        type=Path,
        help=(
            "数据源注册表（deploy/data-sources.json）。给了就校验目标位置已注册，"
            "在本地快速失败而不是等 RAM 报 AccessDenied"
        ),
    )
    archive.add_argument(
        "--via",
        choices=("client", "dataflow"),
        default="client",
        help=(
            "client：本进程逐个文件上传，任何环境可用。"
            "dataflow：交给 CPFS 数据流动做沉淀（Export），服务端并行、"
            "不需要本进程读数据；但目标前缀由 DataFlow 绑定推导，--prefix 会被忽略"
        ),
    )
    archive.add_argument("--cpfs-filesystem-id", help="--via dataflow 时必填")
    archive.add_argument("--cpfs-mount-prefix", help="--via dataflow 时必填")
    archive.add_argument("--region", help="--via dataflow 时必填")
    archive.add_argument("--profile", help="aliyun CLI profile")
    archive.add_argument(
        "--wait-timeout",
        type=int,
        default=7200,
        help="--via dataflow 时等待任务完成的上限秒数（超时按失败处理）",
    )

    commit = commands.add_parser(
        "commit",
        help="从归档前缀零拷贝导入 lakeFS 并产生 Commit（不搬运数据）",
    )
    commit.add_argument("--repository", required=True)
    commit.add_argument("--branch", default="main")
    commit.add_argument(
        "--object-store-uri",
        required=True,
        help="桶级 URI，如 s3://my-bucket。与 --prefix 拼成 import 源",
    )
    commit.add_argument("--prefix", required=True, help="与 archive 时使用的前缀一致")
    commit.add_argument("--destination", required=True, help="Commit 内的目标路径")
    commit.add_argument("--manifest", type=Path, required=True)
    commit.add_argument("--message")
    commit.add_argument("--tag", help="同名 Tag 已存在会报错，不会静默覆盖")
    commit.add_argument("--paimon-snapshot-id")
    commit.add_argument("--lakefs-api-endpoint")
    commit.add_argument("--lakefs-access-key-id")
    commit.add_argument("--lakefs-secret-access-key")

    certify = commands.add_parser(
        "certify",
        help="atomically publish an already prepared CPFS directory without copying",
    )
    certify.add_argument("--prepared-dir", type=Path, required=True)
    certify.add_argument("--target-root", type=Path, required=True)
    certify.add_argument("--dataset", required=True)
    certify.add_argument("--repository", required=True)
    certify.add_argument("--commit", required=True)
    certify.add_argument("--source-reference", required=True)
    certify.add_argument("--manifest", type=Path, required=True)
    certify.add_argument("--lakefs-tag")
    certify.add_argument("--paimon-snapshot-id")

    reclaim = commands.add_parser(
        "reclaim",
        help="回收 CPFS 上不再需要的 release（默认 dry-run，--execute 才真删）",
    )
    reclaim.add_argument("target_root", type=Path, help="CPFS 上的数据集根目录")
    reclaim.add_argument(
        "--min-age-days",
        type=int,
        default=14,
        help=(
            "保护期：发布未满这么多天的一律不回收。未配置占用探测时，"
            "这是唯一挡在回收和运行中训练之间的东西，不要调小（默认 14）"
        ),
    )
    reclaim.add_argument(
        "--keep-last",
        type=int,
        default=2,
        help="每个数据集至少保留最近几个版本，保证不会被清空（默认 2）",
    )
    reclaim.add_argument(
        "--reclaim-bytes",
        type=int,
        help="只回收到腾出这么多字节为止，从最旧的开始。不给则回收全部符合条件的",
    )
    reclaim.add_argument(
        "--include-incomplete",
        action="store_true",
        help="连缺少 _READY 的目录一起回收（发布中断的残骸）。默认不碰",
    )
    reclaim.add_argument(
        "--assume-recoverable",
        action="store_true",
        help=(
            "跳过「删了能否重建」检查。这是整个流程里唯一能造成不可逆数据丢失的"
            "开关，只有在你另有依据确认归档还在时才用"
        ),
    )
    reclaim.add_argument("--lakefs-api-endpoint")
    reclaim.add_argument("--lakefs-access-key-id")
    reclaim.add_argument("--lakefs-secret-access-key")
    reclaim.add_argument(
        "--strategy",
        choices=("hard-delete", "cpfs-evict"),
        default="hard-delete",
        help=(
            "hard-delete：目录整个删掉，PAI 版本会指向不存在的路径，再训练要重跑 "
            "materialize；任何 POSIX 文件系统可用。"
            "cpfs-evict：用 CPFS 数据流动释放数据块、保留元数据，PAI 版本仍然有效、"
            "访问时按需从 OSS 加载；要求路径已被某个 DataFlow 管理，且灵骏 BMCPFS "
            "不支持 Evict。"
        ),
    )
    reclaim.add_argument("--cpfs-filesystem-id", help="--strategy cpfs-evict 时必填")
    reclaim.add_argument(
        "--cpfs-mount-prefix",
        help=(
            "CPFS 挂载点，用于把挂载视角路径换算成文件系统内部路径。"
            "如挂载在 /mnt/cpfs 就填 /mnt/cpfs。填错会作用到错误的目录上"
        ),
    )
    reclaim.add_argument("--region", help="--strategy cpfs-evict 时必填")
    reclaim.add_argument("--profile", help="aliyun CLI profile")
    reclaim.add_argument("--sweep-trash", action="store_true", help="顺带清掉 .trash 里的残骸")
    reclaim.add_argument(
        "--execute",
        action="store_true",
        help="真正删除。不给这个参数只输出计划，不动任何文件",
    )

    verify = commands.add_parser("verify", help="verify release metadata and ready marker")
    verify.add_argument("release_dir", type=Path)
    verify.add_argument(
        "--deep",
        action="store_true",
        help="also hash every materialized file (expensive for large datasets)",
    )

    pai = commands.add_parser("pai-request", help="build a PAI CreateDatasetVersion request")
    pai.add_argument("release_dir", type=Path)
    pai.add_argument("--dataset-id", required=True)
    pai.add_argument("--region", required=True)
    pai.add_argument("--filesystem-id", required=True)
    pai.add_argument(
        "--filesystem-path",
        help="path inside CPFS; defaults to the materializer's release path",
    )
    pai.add_argument("--uri", required=True)
    pai.add_argument(
        "--data-source-type",
        choices=("CPFS", "BMCPFS", "NAS"),
        default="CPFS",
        help=(
            "必须与父 Dataset 的 DataSourceType 一致，否则 PAI 报 "
            "DataSourceType not match。NAS 用于测试/预发环境；"
            "生产训练走 CPFS 以获得并行文件系统吞吐。"
        ),
    )
    pai.add_argument("--protocol-service-id")
    pai.add_argument("--export-id")
    pai.add_argument("--mount-target")
    pai.add_argument("--vpc-mount", action="store_true")
    pai.add_argument("--output", type=Path)

    register = commands.add_parser(
        "register-pai",
        help="register a generated dataset version request through Alibaba Cloud CLI",
    )
    register.add_argument("request_file", type=Path)
    register.add_argument("--region", required=True)
    register.add_argument("--profile")
    register.add_argument(
        "--execute",
        action="store_true",
        help="perform the mutation; without this flag only aliyun --dryrun is used",
    )
    register.add_argument("--aliyun-cli", default="aliyun")

    guard = commands.add_parser(
        "training-guard",
        help="fail closed unless a mounted PAI dataset matches its immutable release",
    )
    guard.add_argument(
        "--dataset-root",
        type=Path,
        default=Path(os.getenv("DATASET_ROOT", "/mnt/dataset")),
    )
    guard.add_argument("--expected-commit", default=os.getenv("DATASET_COMMIT"))
    guard.add_argument(
        "--expected-manifest-sha256",
        default=os.getenv("DATASET_MANIFEST_SHA256"),
    )
    guard.add_argument(
        "--expected-paimon-snapshot-id",
        default=os.getenv("PAIMON_SNAPSHOT_ID"),
    )
    guard.add_argument("--deep", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "materialize":
            result = _materialize(args)
        elif args.command == "scan":
            result = _scan(args)
        elif args.command == "scan-oss":
            result = _scan_oss(args)
        elif args.command == "archive":
            result = _archive(args)
        elif args.command == "commit":
            result = _commit(args)
        elif args.command == "certify":
            result = asdict(
                certify_prepared_release(
                    prepared_dir=args.prepared_dir,
                    target_root=args.target_root,
                    dataset=args.dataset,
                    repository=args.repository,
                    source_reference=args.source_reference,
                    commit_id=args.commit,
                    manifest=Manifest.load(args.manifest),
                    lakefs_tag=args.lakefs_tag,
                    paimon_snapshot_id=args.paimon_snapshot_id,
                )
            )
        elif args.command == "reclaim":
            result = _reclaim(args)
        elif args.command == "verify":
            result = asdict(verify_release(args.release_dir, deep=args.deep))
        elif args.command == "pai-request":
            result = _pai_request(args)
        elif args.command == "register-pai":
            request = json.loads(args.request_file.read_text(encoding="utf-8"))
            result = register_pai_dataset_version(
                request,
                region=args.region,
                profile=args.profile,
                execute=args.execute,
                cli_path=args.aliyun_cli,
            )
        else:
            if not args.expected_commit:
                raise ValueError("training-guard requires --expected-commit or DATASET_COMMIT")
            result = validate_training_dataset(
                args.dataset_root,
                expected_commit=args.expected_commit,
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_paimon_snapshot_id=args.expected_paimon_snapshot_id,
                deep=args.deep,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (DatasetSinkError, ValueError, OSError) as exc:
        print(f"dataset-sink: {exc}", file=sys.stderr)
        return 2


def _scan(args: argparse.Namespace) -> dict:
    result = scan_staging(args.staging_dir, workers=args.workers)
    dump_manifest(result.entries, args.output)
    # 立刻回读一次，让输出里的 manifest_sha256 就是下游会用到的那一个，
    # 而不是在这里重算一遍（重算容易和 dump 的序列化细节不一致）。
    manifest = Manifest.load(args.output)
    payload = summarize_entries(result.entries)
    payload.update(
        {
            "manifest": str(args.output),
            "manifest_sha256": manifest.sha256,
        }
    )
    return payload


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _oss_credentials(args: argparse.Namespace) -> dict:
    """凭证回落顺序：显式参数 → OSS_* → ALIBABA_CLOUD_*。

    最后一组是关键：流水线里 configure-aliyun-credentials-action 通过 OIDC
    假设 RAM 角色后注入的正是 ALIBABA_CLOUD_* 三件套，只认 OSS_* 会导致
    CI 里拿不到凭证。
    """
    return {
        "access_key_id": args.access_key_id
        or _first_env("OSS_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_ID"),
        "secret_access_key": args.secret_access_key
        or _first_env("OSS_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"),
        "security_token": args.security_token
        or _first_env("OSS_SECURITY_TOKEN", "ALIBABA_CLOUD_SECURITY_TOKEN"),
    }


def _scan_oss(args: argparse.Namespace) -> dict:
    registered = None
    if args.registry and args.source == "oss":
        # 只读扫描：位置注册过即可，不要求可写。
        registered = load_registry(args.registry).resolve(args.bucket or "", args.prefix)

    if args.source == "local":
        if args.local_root is None:
            raise ValueError("--source local 需要 --local-root")
        reader = LocalObjectReader(args.local_root)
    else:
        if not args.bucket or not args.endpoint_url:
            raise ValueError("--source oss 需要 --bucket 与 --endpoint-url")
        reader = OssObjectReader(
            bucket=args.bucket,
            endpoint_url=args.endpoint_url,
            verify_tls=not args.no_verify_tls,
            **_oss_credentials(args),
        )

    result = scan_object_store(
        reader,
        args.prefix,
        args.destination,
        with_digest=not args.no_digest,
        workers=args.workers,
    )
    dump_manifest(result.entries, args.output)
    manifest = Manifest.load(args.output)

    payload = summarize_entries(result.entries)
    payload.update(
        {
            "manifest": str(args.output),
            "manifest_sha256": manifest.sha256,
            "prefix": args.prefix,
            "data_source": registered.name if registered else None,
            "destination": validate_destination(args.destination),
            "integrity": "SHA256" if result.digested else "SIZE_ONLY",
        }
    )
    if not result.digested:
        payload["warning"] = (
            "本次未计算 SHA-256。用这份 manifest 发布出来的 release 无法通过 "
            "verify --deep 与 training-guard --deep 做内容校验，且 manifest 随发布"
            "固化、事后无法补算。正式发布前请去掉 --no-digest 重跑。"
        )
    return payload


def _archive(args: argparse.Namespace) -> dict:
    manifest = Manifest.load(args.manifest)

    if args.registry and args.target == "oss":
        # 归档是写操作，所以要求 mode = archive；写进只读数据源会让已发布的
        # Commit 悬空，而那种损坏当时不报错。
        load_registry(args.registry).assert_writable(args.bucket or "", args.prefix)

    if args.via == "dataflow":
        return _archive_via_dataflow(args, manifest)

    if args.target == "local":
        if args.local_root is None:
            raise ValueError("--target local 需要 --local-root")
        writer = LocalObjectWriter(args.local_root)
    else:
        if not args.bucket or not args.endpoint_url:
            raise ValueError("--target oss 需要 --bucket 与 --endpoint-url")
        writer = OssObjectWriter(
            bucket=args.bucket,
            endpoint_url=args.endpoint_url,
            verify_tls=not args.no_verify_tls,
            **_oss_credentials(args),
        )

    result = archive_staging(
        args.staging_dir,
        manifest,
        writer,
        prefix=args.prefix,
        workers=args.workers,
    )
    return {
        "uploaded": result.uploaded,
        "skipped_existing": result.skipped_existing,
        "total_bytes": result.total_bytes,
        "prefix": result.prefix,
        "manifest_sha256": manifest.sha256,
    }


def _commit(args: argparse.Namespace) -> dict:
    manifest = Manifest.load(args.manifest)
    destination = validate_destination(args.destination)
    uri = object_store_uri_for(args.object_store_uri, args.prefix)

    # scan-oss 产出的 manifest 里 source_key 是 Commit 内路径，必须和这里的
    # destination 对得上；对不上就在建 Commit 之前停下，而不是等 materialize
    # 全量 404。cpfs-ingest 的 manifest 不属于这一类，跳过。
    if all(entry.source_key != entry.target_path for entry in manifest.entries):
        assert_manifest_matches_destination(manifest, destination)

    metadata = build_commit_metadata(
        manifest=manifest,
        paimon_snapshot_id=args.paimon_snapshot_id,
        # 记下可读前缀，供后续用 CPFS 数据流动预热这个 Commit。
        object_store_uri=uri,
    )
    message = args.message or (
        f"dataset-sink import {destination} "
        f"({len(manifest.entries)} objects, manifest {manifest.sha256[:12]})"
    )

    result = import_and_commit(
        repository=args.repository,
        branch=args.branch,
        object_store_uri=uri,
        destination=destination,
        message=message,
        metadata=metadata,
        tag=args.tag,
        endpoint=args.lakefs_api_endpoint or os.getenv("LAKEFS_API_ENDPOINT"),
        access_key_id=args.lakefs_access_key_id or os.getenv("LAKEFS_ACCESS_KEY_ID"),
        secret_access_key=(args.lakefs_secret_access_key or os.getenv("LAKEFS_SECRET_ACCESS_KEY")),
    )
    return {
        "commit_id": result.commit_id,
        "branch": result.branch,
        "tag": result.tag,
        "ingested_objects": result.ingested_objects,
        "object_store_uri": result.object_store_uri,
        "destination": destination,
        "manifest_sha256": manifest.sha256,
        "object_store_uri_recorded": uri,
    }


def _dataflow_from(args: argparse.Namespace) -> CpfsDataFlow:
    missing = [
        name
        for name, value in (
            ("--cpfs-filesystem-id", args.cpfs_filesystem_id),
            ("--cpfs-mount-prefix", args.cpfs_mount_prefix),
            ("--region", args.region),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"--via dataflow 需要: {', '.join(missing)}")
    return CpfsDataFlow(
        filesystem_id=args.cpfs_filesystem_id,
        region=args.region,
        mount_prefix=args.cpfs_mount_prefix,
        profile=args.profile,
    )


def _archive_via_dataflow(args: argparse.Namespace, manifest: Manifest) -> dict:
    """沉淀：把 CPFS staging 目录 Export 到 OSS。

    目标前缀**不由调用方指定**，而是从 DataFlow 的 FileSystemPath ↔
    SourceStoragePath 绑定推导出来的——数据流动把两个命名空间死绑在一起，
    OSS 布局必须镜像 CPFS 布局。所以 `--prefix` 在这条路径上没有意义，
    这里显式忽略并把真实落点回报出去，免得使用者以为自己控制了它。
    """
    df = _dataflow_from(args)
    inner = df.filesystem_path(str(args.staging_dir))
    uri = df.object_uri_for(inner)

    task = df.flush(str(args.staging_dir))
    final = df.wait(task.task_id, timeout_seconds=args.wait_timeout)

    return {
        "via": "dataflow",
        "task_id": final.task_id,
        "status": final.status,
        "dataflow_id": final.dataflow_id,
        "filesystem_path": inner,
        # 这个 URI 直接喂给 commit --object-store-uri
        "object_store_uri": uri,
        "manifest_sha256": manifest.sha256,
        # 沉淀是复制不是移动，必须说清楚，否则容易以为空间已经腾出来了。
        "note": (
            "沉淀只把数据复制到 OSS，CPFS 上那份还在、空间没有释放。"
            "要腾容量得再跑 reclaim --strategy cpfs-evict。"
        ),
        "ignored_prefix": args.prefix or None,
    }


def _materialize_via_dataflow(args: argparse.Namespace, manifest: Manifest, commit_id: str) -> dict:
    """预热：让 CPFS 从 OSS 把这个 Commit 的数据拉进来，然后校验发布。

    与 `--via client` 的关键差别：数据不经过本进程，所以执行者**不需要挂载
    CPFS 来搬数据**——但仍然需要能访问 CPFS 来做校验和原子发布。

    落点由 DataFlow 绑定决定，我们只能选目录、不能选映射。所以这里先把数据
    拉进 `.materializing/<commit>/`（它必须也在同一个 DataFlow 覆盖范围内），
    再走 certify 的全量校验 + 同文件系统 rename，发布协议不变。
    """
    df = _dataflow_from(args)
    staging = Path(args.target_root) / ".materializing" / args.dataset / commit_id
    # 在提交任务之前就确认这个路径被某个 DataFlow 覆盖，并算出它对应的 OSS
    # 前缀。放在前面是为了让「暂存目录不在 DataFlow 范围内」这类配置错误立刻
    # 失败，而不是提交一个注定拉不到东西的任务再等它超时。
    inner = df.filesystem_path(str(staging))
    source_uri = df.object_uri_for(inner)

    task = df.prefetch(str(staging))
    final = df.wait(task.task_id, timeout_seconds=args.wait_timeout)

    # 数据流动只保证字节到位，不保证内容与 manifest 一致，所以照常全量校验。
    result = certify_prepared_release(
        prepared_dir=staging,
        target_root=args.target_root,
        dataset=args.dataset,
        repository=args.repository,
        source_reference=args.ref or commit_id,
        commit_id=commit_id,
        manifest=manifest,
        lakefs_tag=args.lakefs_tag or args.ref,
        paimon_snapshot_id=args.paimon_snapshot_id,
    )
    payload = asdict(result)
    payload.update(
        {
            "via": "dataflow",
            "task_id": final.task_id,
            "prefetch_status": final.status,
            "prefetched_from": source_uri,
            "staging_filesystem_path": inner,
        }
    )
    return payload


def _materialize(args: argparse.Namespace) -> dict:
    access_key = args.lakefs_access_key_id or os.getenv("LAKEFS_ACCESS_KEY_ID")
    secret_key = args.lakefs_secret_access_key or os.getenv("LAKEFS_SECRET_ACCESS_KEY")

    if args.commit:
        commit_id = args.commit
        source_reference = args.commit
    else:
        api_endpoint = args.lakefs_api_endpoint or os.getenv("LAKEFS_API_ENDPOINT")
        if not api_endpoint or not access_key or not secret_key:
            raise ValueError(
                "--ref requires lakeFS API endpoint and credentials via arguments or environment"
            )
        commit_id = resolve_reference(
            repository=args.repository,
            reference=args.ref,
            endpoint=api_endpoint,
            access_key_id=access_key,
            secret_access_key=secret_key,
        )
        source_reference = args.ref

    manifest = Manifest.load(args.manifest)

    if args.via == "dataflow":
        return _materialize_via_dataflow(args, manifest, commit_id)

    if args.source == "local":
        if args.local_source_root is None:
            raise ValueError("local source requires --local-source-root")
        source = LocalSourceReader(args.local_source_root)
    else:
        endpoint = args.lakefs_s3_endpoint or os.getenv("LAKEFS_S3_ENDPOINT")
        if not endpoint or not access_key or not secret_key:
            raise ValueError("lakefs-s3 source requires endpoint and lakeFS credentials")
        source = LakeFSS3SourceReader(
            repository=args.repository,
            endpoint_url=endpoint,
            access_key_id=access_key,
            secret_access_key=secret_key,
            region=args.lakefs_region,
            verify_tls=not args.no_verify_tls,
            path_prefix=args.commit_prefix,
        )

    result = Materializer(args.target_root, source, workers=args.workers).materialize(
        dataset=args.dataset,
        repository=args.repository,
        source_reference=source_reference,
        commit_id=commit_id,
        manifest=manifest,
        lakefs_tag=args.lakefs_tag or args.ref,
        paimon_snapshot_id=args.paimon_snapshot_id,
    )
    return asdict(result)


def _reclaim(args: argparse.Namespace) -> dict:
    releases = scan_releases(args.target_root)

    if args.assume_recoverable:
        probe = AssumeRecoverable()
    else:
        endpoint = args.lakefs_api_endpoint or os.getenv("LAKEFS_API_ENDPOINT")
        key = args.lakefs_access_key_id or os.getenv("LAKEFS_ACCESS_KEY_ID")
        secret = args.lakefs_secret_access_key or os.getenv("LAKEFS_SECRET_ACCESS_KEY")
        if not (endpoint and key and secret):
            raise ValueError(
                "回收前必须确认「删了能重建」，这需要 lakeFS 凭证来核对 Commit 是否还在。"
                "提供 --lakefs-api-endpoint 与凭证（或对应环境变量），"
                "或在确有依据时显式加 --assume-recoverable。"
            )
        probe = LakeFSCommitProbe(_lakefs_commit_exists(endpoint, key, secret))

    if args.strategy == "cpfs-evict":
        missing = [
            name
            for name, value in (
                ("--cpfs-filesystem-id", args.cpfs_filesystem_id),
                ("--cpfs-mount-prefix", args.cpfs_mount_prefix),
                ("--region", args.region),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"--strategy cpfs-evict 需要: {', '.join(missing)}")
        strategy = CpfsEvictStrategy(
            filesystem_id=args.cpfs_filesystem_id,
            region=args.region,
            mount_prefix=args.cpfs_mount_prefix,
            profile=args.profile,
        )
    else:
        strategy = HardDeleteStrategy(args.target_root)

    plan = plan_reclaim(
        releases,
        now=datetime.now(timezone.utc),
        min_age_days=args.min_age_days,
        keep_last=args.keep_last,
        reclaim_bytes=args.reclaim_bytes,
        include_incomplete=args.include_incomplete,
        recoverability_probe=probe,
    )
    result = execute_plan(plan, args.target_root, execute=args.execute, strategy=strategy)

    payload: dict = {
        "status": "EXECUTED" if args.execute else "DRY_RUN",
        "strategy": result.strategy,
        "scanned": len(releases),
        "reclaim": [
            {
                "release": d.release.label,
                "size_bytes": d.release.size_bytes,
                "created_at": d.release.created_at.isoformat() if d.release.created_at else None,
                "reason": d.reason,
            }
            for d in plan.reclaim
        ],
        "retain": [{"release": d.release.label, "reason": d.reason} for d in plan.retain],
        "reclaimable_bytes": plan.reclaimable_bytes,
        "reclaimable_gib": round(plan.reclaimable_bytes / (1024**3), 3),
        "freed_bytes": result.freed_bytes,
        "reclaimed": [{"release": r, "outcome": note} for r, note in result.reclaimed],
        "skipped": [{"release": r, "reason": why} for r, why in result.skipped],
    }
    if args.sweep_trash:
        count, swept = sweep_trash(args.target_root, execute=args.execute)
        payload["trash_leftovers"] = count
        payload["trash_swept"] = bool(swept)
    if not args.execute and plan.reclaim:
        payload["note"] = "这是 dry-run，什么都没删。确认无误后加 --execute。"
    return payload


def _lakefs_commit_exists(endpoint: str, key: str, secret: str):
    """返回一个 (repository, commit_id) -> bool 的可调用对象。

    延迟到真正需要时才 import lakefs：核心逻辑保持零运行时依赖。
    """

    def check(repository: str, commit_id: str) -> bool:
        try:
            import lakefs
            from lakefs.client import Client
        except ImportError as exc:
            raise DatasetSinkError("核对 Commit 需要 `pip install -e '.[lakefs]'`") from exc

        client = Client(host=endpoint, username=key, password=secret)
        repo = lakefs.Repository(repository, client=client)
        try:
            repo.ref(commit_id).get_commit()
        except Exception:  # noqa: BLE001 - 查不到就是不存在
            return False
        return True

    return check


def _pai_request(args: argparse.Namespace) -> dict:
    request = build_create_dataset_version_request(
        args.release_dir,
        CpfsRegistration(
            dataset_id=args.dataset_id,
            region=args.region,
            filesystem_id=args.filesystem_id,
            uri=args.uri,
            filesystem_path=args.filesystem_path,
            data_source_type=args.data_source_type,
            protocol_service_id=args.protocol_service_id,
            export_id=args.export_id,
            mount_target=args.mount_target,
            is_vpc_mount=True if args.vpc_mount else None,
        ),
    )
    if args.output:
        args.output.write_text(
            json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return request


if __name__ == "__main__":
    raise SystemExit(main())
