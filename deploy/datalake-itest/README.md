# Data lake integration environment

This directory validates the smallest production-shaped path before the physical storage is delivered:

`S3 client -> JuiceFS S3 Gateway -> shared JuiceFS namespace -> five-node MinIO`, followed by
`Airflow -> Spark Operator -> four CPU nodes -> Iceberg -> private OSS archive`.

The environment also deploys lakeFS as an optional release-control layer. The test writes a release manifest
through its S3 gateway, creates a commit and tag, then reads the object back by immutable commit ID. Spark does
not depend on lakeFS, so disabling it leaves the processing path unchanged.

The reproducible experiment procedure, evidence checklist and known-issue log are in
[`docs/datalake-itest-runbook.md`](../../docs/datalake-itest-runbook.md).

The ACK Pro cluster and buckets are declared in `infra/tests/datalake`. Terraform changes are planned and
applied only by the repository workflow. The deployment workflow obtains a short-lived ACK kubeconfig and
short-lived Alibaba Cloud STS credentials through GitHub OIDC; no long-lived AccessKey is stored. The temporary
session is placed in a Kubernetes Secret only for the duration of the test and is refreshed on every deploy.

## Boundaries

- Four CPU nodes run the control and data-processing workloads.
- Five tainted storage nodes run MinIO. Three of them also run one JuiceFS S3 Gateway replica and one of the
  three-member etcd metadata replicas, matching an integrated storage appliance's protocol placement.
- Each MinIO member owns a 200 GiB ESSD-backed PVC. This validates topology and failures, not HDD performance.
- The five MinIO members form one underlying object store. Applications must not write directly to the internal
  `juicefs-data` bucket; S3 traffic enters through the JuiceFS gateway so it shares one namespace with file access.
- A privileged JuiceFS mount pod validates the POSIX side of the shared namespace. Actual NFS wire-protocol and
  failover testing is a separate gate built on the same mounted namespace.
- The four OSS buckets and ACK cluster are disposable integration resources, not production data stores.
- Spark validates append, current-snapshot read, historical-snapshot read and result publication.
- The OSS credential Secret expires with the CI session and is refreshed by every deploy run.
- The first run uses one million generated rows. Increase `row_count` only after the smoke test succeeds.
- lakeFS runs on CPU nodes with PostgreSQL metadata. Its block store uses the same S3-compatible endpoint
  abstraction that will point to H3C in production.
- The on-premises H3C mixed-flash implementation remains outside this gate. MinIO and JuiceFS validate
  object/file namespace interoperability, while vendor-specific behavior is tested after delivery.

## Execution

1. Run the `Terraform` workflow with target `itest-datalake`, review the plan, then approve its apply job.
2. Run `Data lake integration test` with `deploy-and-test`.
3. Keep the environment only while testing. Remove it with `Destroy data lake integration environment` and the
   exact confirmation string; never delete individual resources in the console.

ACK must be activated for the account and its default service role must exist before the first apply. The
dedicated `TerraformITestApplyRole` in `infra/bootstrap` must be bootstrapped by an administrator once.
