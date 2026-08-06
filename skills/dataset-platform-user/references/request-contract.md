# Request contract

## Existing-version training

Workflow: `pai-runtime.yml`

User fields:

- `runtime`: `dsw` or `dlc`
- `dataset`: catalog short name
- `commit`: immutable lakeFS Commit
- `image_profile`: approved profile name
- `compute_profile`: approved profile name
- `execute`: default `false`

Platform-resolved fields include PAI Dataset ID/version, ACR digest, RAM Role, Region, Workspace,
VPC/vSwitch, security group, mounts, SSH/public-network policy, idle timeout, and maximum runtime.

Mount contract:

| Path | Access | Purpose |
|---|---|---|
| `/mnt/dataset` | RO | Immutable training input |
| `/mnt/workspace` | RW | DSW personal workspace |
| `/mnt/output` | RW | DLC output and checkpoints |

## OSS adoption

Workflow: `dataset-release.yml`

Inputs:

```text
mode=oss-ingest
transfer_mode=dataflow
dataset=<short-name>
repository=<lakefs-repository>
ref=<new-release-tag>
source_bucket=<parsed from oss:// URI>
source_prefix=<parsed from oss:// URI>
```

Reject an unregistered source, mutable ref, empty prefix, arbitrary PAI Dataset ID, or direct raw-OSS
training request.

## CPFS adoption

Workflow: `dataset-release.yml`

Inputs:

```text
mode=cpfs-adopt
transfer_mode=dataflow
dataset=<short-name>
repository=<lakefs-repository>
ref=<new-release-tag>
prepared_dir=<existing-cpfs-directory>
archive_prefix=<archive-prefix>
```

DataFlow `Export` sinks CPFS bytes to OSS. lakeFS import creates the immutable Commit. DataFlow
`Import` preheats the new CPFS release when the path mapping is covered. Manifest/SHA-256 checks
remain mandatory.

## Lifecycle

Workflow: `dataset-lifecycle.yml`

Default inputs:

```text
min_age_days=14
keep_last=2
execute=false
```

Scheduled runs only create plans. Actual Evict requires an administrator, a fresh usage check, and
GitHub Environment approval.

## Audit

Workflow: `pai-mount-audit.yml`; read-only. Use it to identify mutable mounts, unregistered PAI
versions, identity mismatches, or direct workspace mounts.

