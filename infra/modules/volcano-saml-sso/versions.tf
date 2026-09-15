terraform {
  required_version = ">= 1.5.0"

  required_providers {
    # 火山 provider 仍在 0.x（当前 0.0.196），**没有语义化版本承诺**：
    # 小版本之间出现过字段重命名和行为变化。所以这里锁到补丁级，
    # 升级必须是一次显式的、单独 review 的改动，而不是 `terraform init -upgrade`
    # 顺手带上来的。
    volcengine = {
      source  = "volcengine/volcengine"
      version = "~> 0.0.196"
    }
  }
}
