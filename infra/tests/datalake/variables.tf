variable "region" {
  description = "Alibaba Cloud region used by the disposable data-lake integration environment."
  type        = string
  default     = "cn-hangzhou"
}

variable "zone_id" {
  description = "Availability zone for the disposable ACK test cluster. Verify ECS inventory before apply."
  type        = string
  default     = "cn-hangzhou-k"
}

variable "cluster_name" {
  description = "Name of the disposable ACK Pro integration cluster."
  type        = string
  default     = "dataset-sink-datalake-itest"
}

variable "kubernetes_version" {
  description = "ACK-supported Kubernetes version. Empty lets ACK choose the current default."
  type        = string
  default     = ""
}

variable "system_instance_types" {
  description = "Fallback-ordered ECS instance types for the three platform nodes."
  type        = list(string)
  default     = ["ecs.g8i.xlarge", "ecs.g7.xlarge", "ecs.c8i.2xlarge"]
}

variable "spark_instance_types" {
  description = "Fallback-ordered ECS instance types for the three Spark worker nodes."
  type        = list(string)
  default     = ["ecs.g8i.2xlarge", "ecs.g7.2xlarge", "ecs.c8i.4xlarge"]
}

variable "system_node_count" {
  description = "Number of platform nodes. Three keeps Airflow, lakeFS and PostgreSQL available during one-node tests."
  type        = number
  default     = 3
}

variable "spark_node_count" {
  description = "Initial number of Spark nodes."
  type        = number
  default     = 3
}

variable "bucket_prefix" {
  description = "Globally unique OSS prefix. The account ID and layer suffix are appended automatically."
  type        = string
  default     = "dataset-sink-itest"

  validation {
    condition     = can(regex("^dataset-sink-[a-z0-9-]+$", var.bucket_prefix))
    error_message = "bucket_prefix must start with dataset-sink- and contain lowercase letters, digits and hyphens only."
  }
}

variable "tags" {
  description = "Tags applied to disposable test resources."
  type        = map(string)
  default = {
    Project     = "dataset-sink"
    Environment = "itest"
    ManagedBy   = "Terraform"
    Ephemeral   = "true"
  }
}
