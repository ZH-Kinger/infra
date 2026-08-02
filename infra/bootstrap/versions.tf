terraform {
  required_version = ">= 1.5.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = ">= 1.252.0, < 2.0.0"
    }
  }

  # 刻意使用**本地 state**。
  #
  # 这一层负责创建远端 state 后端本身（OSS 桶 + Tablestore 锁表），
  # 如果它自己也用远端后端，就会陷入「桶要靠 Terraform 建、Terraform
  # 要先有桶」的自举死锁。
  #
  # 代价是这一层的 state 文件只存在于执行者机器上：执行完请把
  # terraform.tfstate 归档到安全的地方（如内部密钥库），不要提交进 git。
  # .gitignore 已经忽略 *.tfstate。
}

provider "alicloud" {
  region = var.region
}
