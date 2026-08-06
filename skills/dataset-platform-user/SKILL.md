---
name: dataset-platform-user
description: Guide users and their agents through the governed Alibaba Cloud training-data platform. Use when a user wants to discover a dataset version, adopt existing OSS or CPFS data, prepare a lakeFS-backed release plan, request a PAI DSW or DLC runtime, understand DataFlow preheat/sink behavior, diagnose access or training-guard failures, or determine which request must be escalated to a platform administrator.
---

# Dataset Platform User

Help a non-admin user reach a safe, reviewable request with as few inputs as possible. Prefer the
operations portal when available. Otherwise produce the exact Workflow name and input summary for
the user to review.

## Start safely

1. Identify the intent: use an existing version, adopt data, start DSW/DLC, or diagnose a failure.
2. Read `references/request-contract.md` for the matching request fields.
3. Read `references/errors-and-escalation.md` only for failures or missing prerequisites.
4. If working inside the project repository, treat `docs/user-guide.md`,
   `deploy/pai/runtime-profiles.json`, and `deploy/data-sources.json` as authoritative.
5. Default every write-capable request to plan-only. Show what will happen before offering execute.

## Enforce the user boundary

- Never create RAM users, policies, AccessKeys, PAI members, Filesets, DataFlows, VPC resources, or
  GitHub secrets for an ordinary user.
- Never run local `terraform apply`, `destroy`, `import`, or state mutations.
- Never put credentials in a prompt, file, command, payload, log, or answer.
- Never train from raw OSS, a lakeFS Branch, `latest`, or a date alias. Require an immutable lakeFS
  Commit and the registered PAI Dataset Version.
- Never allow arbitrary Role ARN, PAI Dataset ID, image URL, VPC, vSwitch, security group, mount
  path, or public-network flag from user input. Resolve these from approved catalogs and Profiles.
- Never silently fall back from CPFS DataFlow to client-side TB-scale copying. Report the missing
  Fileset/DataFlow prerequisite to an administrator.
- Never bypass `_READY`, Manifest, SHA-256, Paimon Snapshot, or `training-guard` checks.

## Route the request

### Use an existing dataset

Require the dataset short name and immutable Commit. Select DSW or DLC plus approved image and
compute Profiles. Keep `/mnt/dataset` read-only; use `/mnt/workspace` or `/mnt/output` for writes.
Generate a `pai-runtime.yml` plan before execution.

### Adopt existing OSS data

Require only dataset, lakeFS Repository, `oss://bucket/prefix`, and release Tag. Do not ask the user
for CPFS, PAI, RAM, network, or region IDs. Confirm the source is registered; otherwise prepare an
administrator escalation. Generate `dataset-release.yml` with `mode=oss-ingest` and
`transfer_mode=dataflow`.

### Adopt existing CPFS data

Require dataset, lakeFS Repository, source directory, release Tag, and archive prefix. Use
`mode=cpfs-adopt` when the existing directory must remain. Require a pre-created Fileset/DataFlow
covering the source path. DataFlow Export performs the sink; publishing still verifies content.

### Diagnose a failure

Classify the failure before suggesting action: identity, catalog, DataFlow, release integrity, PAI
mount, runtime Profile, or training guard. Preserve fail-closed behavior. Give the user the smallest
administrator escalation containing dataset, Commit, operation, region, Workflow run, and exact
error—never credentials.

## Present the result

Always state:

- selected dataset and immutable Commit/Tag;
- chosen Workflow and mode;
- user-provided fields versus platform-resolved defaults;
- plan-only or execute state;
- DataFlow direction (`Import` preheat or `Export` sink), when applicable;
- approval and administrator prerequisites;
- expected read-only and read-write mount paths.

Do not claim success until the Workflow reaches its terminal state and the release or runtime passes
its guard. If no execution was requested, stop at a reviewable plan.

