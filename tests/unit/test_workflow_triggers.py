from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowTriggerIsolationTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return Path(".github/workflows", name).read_text(encoding="utf-8")

    def test_ci_is_pr_or_manual_and_ignores_documentation(self):
        workflow = self._read("ci.yml")
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("  workflow_dispatch:\n", workflow)
        self.assertNotIn("  push:\n", workflow)
        self.assertIn('      - ".github/workflows/**"', workflow)
        self.assertNotIn('      - "README.md"', workflow)
        self.assertNotIn('      - "docs/**"', workflow)

    def test_terraform_apply_is_manual_and_explicit(self):
        workflow = self._read("terraform.yml")
        self.assertIn("  workflow_dispatch:\n", workflow)
        self.assertIn("confirm_apply:", workflow)
        self.assertIn("inputs.confirm_apply", workflow)
        self.assertIn("fromJSON(needs.select.outputs.matrix)", workflow)
        self.assertIn("TARGET: ${{ inputs.target || 'all' }}", workflow)
        self.assertIn("TFVARS_DEV_PLATFORM_JSON", workflow)
        self.assertIn("vars[matrix.tfvars_var]", workflow)
        self.assertIn("python3 -m json.tool", workflow)
        self.assertIn("/tmp/tfplan-artifact/tfplan", workflow)
        self.assertIn('cp "/tmp/artifact/tfplan"', workflow)
        self.assertNotIn("/tmp/artifact/${{ matrix.dir }}/tfplan", workflow)
        self.assertIn("核对 GitHub OIDC 身份声明", workflow)
        self.assertIn("claims.sub !== expectedSub", workflow)
        self.assertNotIn("  push:\n", workflow)

    def test_terraform_plan_pipeline_propagates_failures(self):
        workflow = self._read("terraform.yml")
        plan_step = workflow.split("      - name: Plan\n", 1)[1].split(
            "      - name: 上传 plan 产物\n", 1
        )[0]
        self.assertIn("set -euo pipefail", plan_step)
        self.assertIn("terraform -chdir=${{ matrix.dir }} plan", plan_step)
        self.assertIn("-lock=false", plan_step)
        self.assertIn("| tee /tmp/plan-${{ matrix.layer }}.txt", plan_step)

    def test_cloud_data_workflows_are_manual(self):
        for name in (
            "dataset-release.yml",
            "pai-mount-audit.yml",
            "oidc-role-smoke.yml",
            "cloud-preflight.yml",
            "terraform-force-unlock.yml",
            "terraform-state-forget.yml",
            "pai-runtime.yml",
        ):
            workflow = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("  workflow_dispatch:\n", workflow)
                self.assertNotIn("  push:\n", workflow)
                self.assertNotIn("  schedule:\n", workflow)

    def test_pai_runtime_is_profile_driven_dry_run_by_default(self):
        workflow = self._read("pai-runtime.yml")
        self.assertIn("default: false", workflow)
        self.assertIn("environment: pai-runtime", workflow)
        self.assertIn("runtime-request", workflow)
        self.assertIn("--request-output", workflow)
        self.assertIn("DSW_SUBMIT_ROLE_ARN", workflow)
        self.assertIn("DLC_SUBMIT_ROLE_ARN", workflow)
        self.assertIn("if: ${{ inputs.execute }}", workflow)
        self.assertNotIn("ForwardInfos", workflow)

    def test_existing_cpfs_adoption_preserves_the_source_directory(self):
        workflow = self._read("dataset-release.yml")
        self.assertIn("cpfs-adopt", workflow)
        self.assertIn("inputs.mode == 'cpfs-adopt'", workflow)
        self.assertIn("--commit-prefix 'datasets/${{ inputs.dataset }}'", workflow)
        self.assertIn("PAI_DATASET_IDS_JSON", workflow)
        self.assertIn("needs.preflight.outputs.pai_dataset_id", workflow)

    def test_dataset_release_uses_cpfs_dataflow_by_default(self):
        workflow = self._read("dataset-release.yml")
        self.assertIn("transfer_mode:", workflow)
        self.assertIn("default: dataflow", workflow)
        self.assertIn("--via dataflow", workflow)
        self.assertIn("--cpfs-filesystem-id '${{ vars.CPFS_FILESYSTEM_ID }}'", workflow)
        self.assertIn("--cpfs-mount-prefix '${{ vars.CPFS_MOUNT_PREFIX }}'", workflow)
        self.assertIn("needs.ingest-archive.outputs.object_store_uri", workflow)
        self.assertIn("needs.ingest-archive.outputs.object_store_prefix", workflow)

    def test_lifecycle_is_manual_dry_run_and_execution_is_approved(self):
        workflow = self._read("dataset-lifecycle.yml")
        plan, execute = workflow.split("\n  execute:\n", 1)
        self.assertIn("  workflow_dispatch:\n", plan)
        self.assertNotIn("  schedule:\n", plan)
        self.assertNotIn("--execute", plan)
        self.assertIn("environment: dataset-lifecycle", execute)
        self.assertIn("DATASET_LIFECYCLE_ROLE_ARN", execute)
        self.assertIn("--execute", execute)
        self.assertIn("重新检查并执行", execute)
        self.assertNotIn("hard-delete", workflow)

    def test_force_unlock_is_manual_confirmed_and_uses_apply_role(self):
        workflow = self._read("terraform-force-unlock.yml")
        self.assertIn("confirm_unlock:", workflow)
        self.assertIn("if: ${{ inputs.confirm_unlock }}", workflow)
        self.assertIn("concurrency:\n  group: terraform-${{ github.ref }}", workflow)
        self.assertIn("ALIBABA_CLOUD_PLATFORM_APPLY_ROLE_ARN", workflow)
        self.assertIn("ALIBABA_CLOUD_ACCESS_APPLY_ROLE_ARN", workflow)
        self.assertIn('force-unlock -force "$LOCK_ID"', workflow)
        self.assertNotIn("-lock=false", workflow)

    def test_state_forget_is_manual_confirmed_and_never_deletes_cloud_resources(self):
        workflow = self._read("terraform-state-forget.yml")
        self.assertIn("confirm_forget:", workflow)
        self.assertIn("if: ${{ inputs.confirm_forget }}", workflow)
        self.assertIn('state rm -lock-timeout=5m "$RESOURCE_ADDRESS"', workflow)
        self.assertNotIn("terraform destroy", workflow)
        self.assertNotIn("DeleteBucket", workflow)

    def test_oidc_smoke_only_checks_temporary_credentials(self):
        workflow = self._read("oidc-role-smoke.yml")
        self.assertIn("environment: development", workflow)
        self.assertIn("ALIBABA_CLOUD_PLATFORM_APPLY_ROLE_ARN", workflow)
        self.assertIn("ALIBABA_CLOUD_ACCESS_APPLY_ROLE_ARN", workflow)
        self.assertNotIn("terraform apply", workflow)
        self.assertNotIn("--execute", workflow)

    def test_cloud_preflight_is_readonly_and_pins_cli_checksum(self):
        workflow = self._read("cloud-preflight.yml")
        self.assertIn("ALIBABA_CLOUD_PLAN_ROLE_ARN", workflow)
        self.assertIn("ALIYUN_CLI_LINUX_AMD64_SHA256", workflow)
        self.assertIn("sha256sum --check --strict", workflow)
        self.assertIn("./scripts/preflight.sh", workflow)
        for mutation in ("Create", "Update", "Modify", "Delete", "terraform apply", "--execute"):
            self.assertNotIn(mutation, workflow)

    def test_plan_role_trusts_manual_main_but_remains_readonly(self):
        bootstrap = Path("infra/bootstrap/oidc.tf").read_text(encoding="utf-8")
        variables = Path("infra/bootstrap/variables.tf").read_text(encoding="utf-8")
        self.assertIn("repo:${var.github_oidc_repo}:ref:refs/heads/main", bootstrap)
        self.assertIn("platform_apply_subjects", bootstrap)
        self.assertIn("access_apply_subjects", bootstrap)
        self.assertIn('default     = ["development", "production"]', variables)
        self.assertIn('default     = ["development", "production-access"]', variables)
        self.assertIn("DenyAllMutations", bootstrap)
        plan_policy = bootstrap.split("  plan_policy = jsonencode({", 1)[1].split(
            "  platform_apply_policy = jsonencode({", 1
        )[0]
        self.assertIn('"ots:DeleteRow"', bootstrap)
        self.assertIn('"ots:Delete*"', plan_policy)
        self.assertNotIn("local.state_lock_statement", plan_policy)
        self.assertIn('"oss:GetBucket*"', bootstrap)
        # ACK provider refresh must match the complete read baseline from the
        # AliyunCSReadOnlyAccess system policy, not only cs:Describe*.
        self.assertIn('"cs:CheckServiceRole"', bootstrap)
        self.assertIn('"cs:CheckControlPlaneLogEnable"', bootstrap)
        self.assertIn('"cs:Get*"', bootstrap)
        self.assertIn('"cs:List*"', bootstrap)
        self.assertIn('"cs:Query*"', bootstrap)
        self.assertIn('"cs:Describe*"', bootstrap)

    def test_oidc_subject_uses_immutable_github_ids(self):
        bootstrap = Path("infra/bootstrap/variables.tf").read_text(encoding="utf-8")
        roles = Path("infra/modules/dataset-sink-roles/roles.tf").read_text(encoding="utf-8")
        workflow = self._read("terraform.yml")
        self.assertIn('variable "github_oidc_repo"', bootstrap)
        self.assertIn("repo:${var.github_oidc_repo}:environment:", roles)
        self.assertIn("claims.repository_owner_id", workflow)
        self.assertIn("claims.repository_id", workflow)

    def test_oidc_role_uses_ram_trust_policy_action(self):
        module = Path("infra/modules/ci-oidc-role/main.tf").read_text(encoding="utf-8")
        self.assertIn('Action = "sts:AssumeRole"', module)
        self.assertNotIn('Action = "sts:AssumeRoleWithOIDC"', module)

    def test_itest_role_covers_ack_lifecycle_without_identity_admin(self):
        bootstrap = Path("infra/bootstrap/oidc.tf").read_text(encoding="utf-8")
        policy = bootstrap.split("  itest_apply_policy = jsonencode({", 1)[1].split(
            "  # -------------------------------------------------------------------------\n"
            "  # TerraformAccessApplyRole",
            1,
        )[0]
        self.assertIn('Action   = ["cs:*"]', policy)
        self.assertIn('Action   = ["ram:PassRole"]', policy)
        self.assertIn('"acs:Service" = "cs.aliyuncs.com"', policy)
        self.assertIn('"ram:Create*"', policy)
        self.assertIn('"ram:Delete*"', policy)
        self.assertNotIn('Action   = ["ram:*"', policy)

    def test_datalake_itest_sets_region_for_ack_cli(self):
        workflow = self._read("datalake-itest.yml")
        self.assertIn("ALIBABA_CLOUD_REGION: ${{ vars.ALIBABA_CLOUD_REGION }}", workflow)
        self.assertIn(
            'aliyun --region "$ALIBABA_CLOUD_REGION" cs DescribeClusterUserKubeconfig',
            workflow,
        )

    def test_datalake_itest_uses_essd_and_reachable_image_sources(self):
        root = Path("deploy/datalake-itest")
        minio = (root / "minio.yaml").read_text(encoding="utf-8")
        spark = (root / "spark-iceberg-itest.yaml").read_text(encoding="utf-8")
        airflow = (root / "airflow-values.yaml").read_text(encoding="utf-8")
        airflow_dag = (root / "dags" / "datalake_itest.py").read_text(encoding="utf-8")
        spark_operator = (root / "spark-operator-values.yaml").read_text(encoding="utf-8")
        ivy_settings = (root / "ivysettings.xml").read_text(encoding="utf-8")
        deploy = (root / "deploy.sh").read_text(encoding="utf-8")
        run_test = (root / "run-test.sh").read_text(encoding="utf-8")
        self.assertIn("storageClassName: alicloud-disk-essd", minio)
        self.assertIn("quay.io/minio/mc:", minio)
        self.assertIn("docker.m.daocloud.io/apache/spark:3.5.5", spark)
        self.assertIn("iceberg-spark-runtime-3.5_2.12:1.10.2", spark)
        self.assertNotIn("iceberg-spark-runtime-3.5_2.12:1.11.0", spark)
        spark_job = (root / "jobs" / "iceberg_itest.py").read_text(encoding="utf-8")
        self.assertNotIn("fs.oss.credentials.provider", spark_job)
        self.assertIn("docker.m.daocloud.io/apache/airflow", airflow)
        self.assertIn("docker.m.daocloud.io\n    repository: bitnamilegacy/postgresql", airflow)
        self.assertIn("storageClass: alicloud-disk-essd", airflow)
        self.assertGreaterEqual(airflow.count("storageClassName: alicloud-disk-essd"), 2)
        self.assertIn("repair_airflow_pvcs=false", deploy)
        self.assertIn("pending-install|pending-upgrade|pending-rollback", deploy)
        self.assertIn("helm uninstall airflow --namespace airflow", deploy)
        self.assertIn("migrateDatabaseJob:\n  useHelmHooks: false", airflow)
        self.assertIn("rollout status statefulset/airflow-scheduler", deploy)
        self.assertNotIn("rollout status deployment/airflow-scheduler", deploy)
        self.assertIn("exec statefulset/airflow-scheduler", run_test)
        self.assertIn("logs statefulset/airflow-scheduler", run_test)
        self.assertIn("airflow dags list", run_test)
        self.assertIn("airflow dags list-import-errors", run_test)
        self.assertIn('airflow dags unpause "$dag_id"', run_test)
        self.assertNotIn("deployment/airflow-scheduler", run_test)
        self.assertEqual(airflow.count("name: datalake-itest-dags"), 6)
        self.assertEqual(airflow.count("subPath: datalake_itest.py"), 2)
        self.assertEqual(airflow.count("subPath: spark-iceberg-itest.yaml"), 2)
        self.assertNotIn("mountPath: /opt/airflow/dags/repository", airflow)
        self.assertIn("scheduler:\n  replicas: 2", airflow)
        self.assertIn("dagProcessor:\n  extraVolumes:", airflow)
        self.assertIn('dags_are_paused_at_creation: "False"', airflow)
        self.assertNotIn("_PIP_ADDITIONAL_REQUIREMENTS", airflow)
        self.assertIn('application_file="spark-iceberg-itest.yaml"', airflow_dag)
        self.assertNotIn(
            'application_file="/opt/airflow/dags/spark-iceberg-itest.yaml"',
            airflow_dag,
        )
        self.assertNotIn("ts_nodash", spark)
        self.assertIn("name: iceberg-itest", spark)
        self.assertIn('--from-literal=BATCH_ID="itest-${GITHUB_RUN_ID:-manual}"', deploy)
        self.assertIn("registry: ghcr.m.daocloud.io", spark_operator)
        self.assertIn("https://maven.aliyun.com/repository/public", ivy_settings)
        self.assertIn("spark.jars.ivy: /tmp/spark-ivy-cache", spark)
        self.assertIn("spark.jars.ivySettings: file:///etc/spark-operator/ivysettings.xml", spark)
        self.assertIn("mountPath: /etc/spark-operator/ivysettings.xml", spark_operator)
        self.assertIn("create configmap spark-operator-ivy-settings", deploy)
        self.assertIn("create configmap spark-workload-ivy-settings", deploy)
        self.assertIn("name: spark-workload-ivy-settings", spark)
        self.assertEqual(spark.count("mountPath: /etc/spark-operator/ivysettings.xml"), 2)
        self.assertIn('[ "$phase" = "Pending" ] && [ -z "$storage_class" ]', deploy)
        self.assertNotIn("kubectl -n datalake-itest delete pvc --all", deploy)

    def test_datalake_itest_models_shared_s3_and_file_namespace(self):
        root = Path("deploy/datalake-itest")
        juicefs = (root / "juicefs.yaml").read_text(encoding="utf-8")
        minio = (root / "minio.yaml").read_text(encoding="utf-8")
        deploy = (root / "deploy.sh").read_text(encoding="utf-8")
        run_test = (root / "run-test.sh").read_text(encoding="utf-8")

        self.assertIn("mc mb --ignore-existing local/juicefs-data", minio)
        self.assertIn("name: juicefs-meta\n", juicefs)
        self.assertIn("name: juicefs-s3-gateway\n", juicefs)
        self.assertIn("replicas: 3", juicefs)
        self.assertIn("workload: storage", juicefs)
        self.assertIn("--storage minio", juicefs)
        self.assertIn("/juicefs-data", juicefs)
        self.assertIn("juicefs mount", juicefs)
        self.assertIn("rollout status deployment/juicefs-s3-gateway", deploy)
        self.assertIn("S3 <-> POSIX shared-namespace test completed", run_test)
        self.assertIn('mc pipe "jfs/factory/$OBJECT_KEY"', run_test)
        self.assertIn('mc cat "jfs/factory/$OBJECT_KEY"', run_test)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
