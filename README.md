# lakeFS → CPFS Dataset Sink

Materialize immutable lakeFS commits into versioned CPFS datasets that Alibaba Cloud PAI DSW
and DLC can mount read-only, with the infrastructure and permissions delivered through Terraform
and GitHub Actions.

This project defines a **dataset release boundary**. It does not replace lakeFS with CPFS:

```text
 Existing data in OSS       New data prepared on CPFS       Data already in lakeFS
 scan-oss → commit          scan → archive → commit         existing Commit
 zero-copy import           the only required data copy
          ↓                           ↓                            ↓
                  lakeFS Commit + Paimon Snapshot + Manifest
                                      ↓
                       certify (zero-copy) / materialize (copy)
                                      ↓
                         size and SHA-256 integrity checks
                                      ↓
                    <cpfs-root>/<dataset>/<commit>/_READY
                                      ↓
                         PAI Dataset Version registration
                                      ↓
                    read-only DSW/DLC mount + startup guard
```

Existing OSS data does **not** need to be migrated. A lakeFS import is zero-copy and only records
the physical object location. The tradeoff is that imported prefixes become immutable: deleting an
object can leave a Commit dangling without failing at deletion time.

Object storage is the **durable archive and physical source of truth**. A CPFS release is a
**training-optimized hot copy** that may be evicted and materialized again when needed.

The primary rule is:

> **Every dataset used for training must resolve to a lakeFS Commit hash. Never read raw OSS
> directly, and never mount a mutable branch or `latest`.**

`training-guard` enforces this rule before the training process starts.

---

## Documentation

| Goal | Document |
|---|---|
| Train with a published dataset | [User onboarding](docs/onboarding.md) |
| Start a governed DSW or DLC runtime | [DSW/DLC self-service](docs/pai-runtime.md) |
| Understand the architecture and tradeoffs | [Architecture](docs/architecture.md) |
| Manage OSS/CPFS data, mounts, and retention | [Storage lifecycle](docs/storage-lifecycle.md) |
| Understand permission isolation and privilege escalation controls | [Permission model](docs/permissions.md) |
| Understand pipelines and approvals | [CI/CD](docs/cicd.md) |
| Initialize or troubleshoot an environment | [Operations runbook](docs/runbook.md) |
| Migrate existing CPFS directories into Filesets | [Fileset migration](docs/cpfs-fileset-migration.md) |
| Evaluate or add another region | [Multi-region boundaries](docs/multi-region.md) |
| Contribute to this repository | [Repository conventions](AGENTS.md) |

---

## Quick start

All local checks run without cloud credentials or runtime third-party dependencies:

```bash
make test    # offline unit and contract tests; real-cloud tests are skipped by default
make e2e     # end-to-end simulation: ingest → verify → training guard → reclaim → PAI request
make help    # list all available targets
```

`make e2e` simulates CPFS with a temporary POSIX directory. It does not connect to Alibaba Cloud or
create billable resources.

---

## Command overview

```bash
# Existing OSS data: scan and hash it, then create a lakeFS Commit without copying bytes.
dataset-sink scan-oss --bucket legacy-data \
  --endpoint-url https://oss-cn-hangzhou.aliyuncs.com \
  --prefix legacy/robotics --destination datasets/robotics \
  --output /work/manifest.jsonl

dataset-sink commit --repository robotics-data --branch main \
  --object-store-uri s3://legacy-data \
  --prefix legacy/robotics --destination datasets/robotics \
  --manifest /work/manifest.jsonl --tag robotics-v2026.08.02.1

# New data prepared on CPFS: scan, archive, and create a Commit.
dataset-sink scan /mnt/cpfs/staging/batch-001 --output /work/manifest.jsonl

dataset-sink archive /mnt/cpfs/staging/batch-001 \
  --manifest /work/manifest.jsonl --prefix staging/batch-001 \
  --target oss --bucket dataset-sink-archive \
  --endpoint-url https://oss-cn-hangzhou.aliyuncs.com

dataset-sink commit --repository robotics-data --branch main \
  --object-store-uri s3://dataset-sink-archive \
  --prefix staging/batch-001 --destination datasets/robotics \
  --manifest /work/manifest.jsonl --tag robotics-v2026.08.02.1

# Materialize and publish a fixed lakeFS version.
dataset-sink materialize --dataset robotics --repository robotics-data \
  --ref robotics-v2026.08.02.1 --manifest /work/manifest.jsonl \
  --source lakefs-s3 --target-root /mnt/cpfs/datasets --workers 32

# Data is already staged on CPFS: publish it atomically with a same-filesystem rename.
dataset-sink certify --prepared-dir /mnt/cpfs/staging/batch-001 \
  --target-root /mnt/cpfs/datasets --dataset robotics \
  --repository robotics-data --commit 6f2b7c91c2 \
  --source-reference robotics-v2026.08.02.1 --manifest /work/manifest.jsonl

# Delegate byte transfer to CPFS DataFlow for archive export or materialization prefetch.
# The destination is derived from the DataFlow binding; --prefix does not apply in this mode.
dataset-sink archive /mnt/cpfs/staging/batch-001 --manifest /work/manifest.jsonl \
  --via dataflow --cpfs-filesystem-id cpfs-xxxx --cpfs-mount-prefix /mnt/cpfs \
  --region cn-hangzhou

# Reclaim unused CPFS releases. This is a dry-run unless --execute is explicitly supplied.
dataset-sink reclaim /mnt/cpfs/datasets \
  --lakefs-api-endpoint https://lakefs.internal --min-age-days 14 --keep-last 2 \
  --pai-usage-workspace-id 617398 --pai-usage-region cn-hangzhou

# Verify a release; --deep recalculates every SHA-256 digest.
dataset-sink verify /mnt/cpfs/datasets/robotics/6f2b7c91c2 --deep

# Build a PAI Dataset Version request locally without cloud permissions.
dataset-sink pai-request /mnt/cpfs/datasets/robotics/6f2b7c91c2 \
  --dataset-id d-example --region cn-hangzhou \
  --filesystem-id cpfs-example \
  --filesystem-path /datasets/robotics/6f2b7c91c2 \
  --uri cpfs://cpfs-example.cn-hangzhou/ptc-example/datasets/robotics/6f2b7c91c2/

# Registration is a dry-run by default. --execute performs the idempotent write explicitly.
dataset-sink register-pai /work/pai-request.json --region cn-hangzhou --execute

# Fail-closed startup guard inside the training container.
dataset-sink training-guard --dataset-root /mnt/dataset \
  --expected-commit "$DATASET_COMMIT" \
  --expected-manifest-sha256 "$DATASET_MANIFEST_SHA256"
```

The `--uri` scheme must match `--data-source-type`, or PAI returns `Uri format error`:

| DataSourceType | URI scheme |
|---|---|
| `NAS` | `nas://` |
| `CPFS` | `cpfs://` |
| `BMCPFS` | `bmcpfs://` |

Alibaba Cloud documentation currently describes CPFS with `nas://`, but a real-account test on
2026-08-03 showed that PAI accepts only `cpfs://`. `Property=DIRECTORY` also requires a trailing
slash. Both constraints are validated locally before the API call.

Two coordinate systems are easy to confuse:

- `release_dir` is the client mount path, while `--filesystem-path` is the path inside CPFS.
- Manifest `target_path` is relative to a release, while `source_key` is relative to the
  **Commit**. Therefore, `scan-oss` and `commit` must receive the same `--destination` value.
  Otherwise, `materialize` would return 404 for every object. `commit` rejects this mismatch before
  creating a Commit.

Connecting to a real lakeFS instance requires `pip install -e '.[all]'` and secret injection for
`LAKEFS_API_ENDPOINT`, `LAKEFS_S3_ENDPOINT`, `LAKEFS_ACCESS_KEY_ID`, and
`LAKEFS_SECRET_ACCESS_KEY`. The application does not persist credentials.

---

## Release layout and protocol

```text
/mnt/cpfs/datasets/
├── .locks/                       # process locks
├── .materializing/               # incomplete materializations live only here
├── .trash/                       # releases are renamed here atomically before deletion
└── robotics/
    └── 6f2b7c91c2/               # immutable directory named by lakeFS Commit
        ├── shards/
        ├── manifest.jsonl
        ├── release.json          # commit / manifest_sha256 / paimon_snapshot_id
        ├── _READY                # written last; a release without it is unusable
        └── .keep                 # optional manual pin; reclaim never removes it
```

The CPFS release is the **only intentionally disposable layer**. Before reclaiming it, the tool
must prove that the Commit still exists in lakeFS and can be materialized again. If recoverability
cannot be confirmed, the release is retained. False negatives are preferable to destructive false
positives.

Materializing an existing Commit is an idempotent no-op. Reusing the same Commit with a different
Manifest raises `ReleaseConflictError`; an existing release is never overwritten. PAI may mount
only `<dataset>/<commit>/` paths.

---

## Repository layout

```text
src/dataset_sink/   Python implementation; zero core runtime dependencies
tests/unit/         offline unit tests
tests/integration/  real-environment tests; skipped when opt-in variables are absent
infra/bootstrap/    local state: state backend, OIDC trust anchor, and CI roles
infra/modules/      reusable OIDC, RAM, PAI workspace, and CPFS workspace modules
infra/envs/         dev|prod × platform|access, each with an independent state
deploy/ram/         generated RAM policy copies; do not edit manually
deploy/pai/         DLC job templates and training entrypoint
scripts/            local simulation, policy rendering, discovery, and read-only preflight
docs/               architecture, permissions, CI/CD, operations, and user guidance
```

---

## Identity isolation

Materialization, registration, auditing, job submission, and training runtime are separate trust
levels. Compromising one identity is insufficient to corrupt and consume a dataset end to end:

| Identity | Allowed | Explicitly prohibited |
|---|---|---|
| Materializer | Read a fixed lakeFS Commit and write a CPFS release | Register a PAI version or submit training |
| Register | Create and resolve Dataset Versions | Read the lakeFS backend or submit training |
| DlcSubmit | Submit DLC jobs bound to approved versions | Modify dataset versions or read the lakeFS backend |
| DswSubmit | Create private DSW instances for mapped RAM users | Submit DLC or modify dataset versions |
| PaiMountAudit | Read DLC/DSW mount metadata | Submit jobs, mutate datasets, or read dataset bytes |
| TrainingRuntime | Read published archives and write its own output | Read the lakeFS backend or staging area |
| Developer group | Use published versions | Create long-lived keys or modify releases |

CI authenticates through GitHub OIDC → RAM Role → temporary STS credentials. No long-lived Alibaba
Cloud access keys are stored in the repository or CI configuration. See the
[permission model](docs/permissions.md) for the four authorization planes, privilege-escalation
controls, and platform limitations.

---

## Requirements for a real environment

All logic and tests can run locally. Integration requires an environment with network access to
CPFS and PAI. Provide resource identifiers and temporary authorization only—never send AK/SK:

- Region, PAI Workspace ID, Dataset ID, and DLC Resource/Quota ID
- CPFS/BMCPFS filesystem ID, internal path, VPC mount target, and the PAI VPC/vSwitch
- Internal lakeFS API/S3 Gateway endpoints and a test Repository/Tag, with credentials injected as
  secrets
- A representative Paimon Manifest and Snapshot ID
- A self-hosted runner or ACK Job environment that can mount CPFS

Run `make discover` in the target account to discover most identifiers and generate a tfvars draft.
See section 0 of the [operations runbook](docs/runbook.md) for current blockers and prerequisites.
