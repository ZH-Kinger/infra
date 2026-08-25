# Data lake integration environment

This directory validates the smallest production-shaped path before the physical storage is delivered:

`Airflow -> Spark Operator -> four CPU nodes -> Iceberg -> five-node MinIO -> private OSS archive`.

The reproducible experiment procedure, evidence checklist and known-issue log are in
[`docs/datalake-itest-runbook.md`](../../docs/datalake-itest-runbook.md).

The ACK Pro cluster and buckets are declared in `infra/tests/datalake`. Terraform changes are planned and
applied only by the repository workflow. The deployment workflow obtains a short-lived ACK kubeconfig and
short-lived Alibaba Cloud STS credentials through GitHub OIDC; no long-lived AccessKey is stored. The temporary
session is placed in a Kubernetes Secret only for the duration of the test and is refreshed on every deploy.

## Boundaries

- Four CPU nodes run the control and data-processing workloads; five tainted storage nodes run MinIO only.
- Each MinIO member owns a 200 GiB ESSD-backed PVC. This validates topology and failures, not HDD performance.
- The four OSS buckets and ACK cluster are disposable integration resources, not production data stores.
- Spark validates append, current-snapshot read, historical-snapshot read and result publication.
- The OSS credential Secret expires with the CI session and is refreshed by every deploy run.
- The first run uses one million generated rows. Increase `row_count` only after the smoke test succeeds.
- lakeFS and the on-premises H3C NFS/mixed-flash implementation are intentionally excluded from this first gate.
  MinIO represents only the five-node local S3 contract; vendor-specific behavior is tested after delivery.

## Execution

1. Run the `Terraform` workflow with target `itest-datalake`, review the plan, then approve its apply job.
2. Run `Data lake integration test` with `deploy-and-test`.
3. Keep the environment only while testing. Remove it with `Destroy data lake integration environment` and the
   exact confirmation string; never delete individual resources in the console.

ACK must be activated for the account and its default service role must exist before the first apply. The
dedicated `TerraformITestApplyRole` in `infra/bootstrap` must be bootstrapped by an administrator once.
