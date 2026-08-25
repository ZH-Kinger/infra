terraform {
  required_version = ">= 1.5.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "= 1.286.0"
    }
  }

  backend "oss" {
    key    = "tests/datalake.tfstate"
    prefix = "terraform"
    acl    = "private"
  }

}

provider "alicloud" {
  region = var.region
}
