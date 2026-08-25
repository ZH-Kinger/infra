output "cluster_id" {
  description = "ACK Pro cluster ID used by the deploy and test workflows."
  value       = alicloud_cs_managed_kubernetes.this.id
}

output "bucket_names" {
  description = "Dedicated OSS buckets for the integration environment."
  value       = local.bucket_names
}

output "vpc_id" {
  description = "Dedicated test VPC ID."
  value       = alicloud_vpc.this.id
}

output "node_vswitch_id" {
  description = "Node vSwitch for the disposable ACK cluster."
  value       = alicloud_vswitch.nodes.id
}

output "worker_ram_role_name" {
  description = "ACK-created worker role name (reported for audit; no test-bucket policy is attached)."
  value       = alicloud_cs_managed_kubernetes.this.worker_ram_role_name
}
