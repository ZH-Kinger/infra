from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Sequence

from .aliyun_cli import register_pai_dataset_version
from .errors import DatasetSinkError
from .lakefs_refs import resolve_reference
from .manifest import Manifest
from .materializer import Materializer, certify_prepared_release, verify_release
from .pai import CpfsRegistration, build_create_dataset_version_request
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
    materialize.add_argument("--no-verify-tls", action="store_true")

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
    pai.add_argument("--data-source-type", choices=("CPFS", "BMCPFS"), default="CPFS")
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
        )

    manifest = Manifest.load(args.manifest)
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
