.PHONY: help test compile lint fmt e2e tf-fmt tf-validate hooks discover render-ram all

PYTHON ?= python3
TERRAFORM ?= terraform
export PYTHONPATH := src

help:
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

all: lint test tf-fmt tf-validate ## 提交前跑一遍：lint + 单元测试 + Terraform 校验

test: ## 单元测试（离线，无需云凭证）
	$(PYTHON) -m unittest discover -s tests -v

compile: ## 语法编译检查
	PYTHONPYCACHEPREFIX=/tmp/dataset-sink-pycache $(PYTHON) -m compileall -q src tests

lint: ## ruff check（需先 pip install -e '.[dev]'）
	@$(PYTHON) -m ruff --version >/dev/null 2>&1 || { \
		echo "ruff 未安装，执行: $(PYTHON) -m pip install -e '.[dev]'"; exit 1; }
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

fmt: ## 格式化 Python 与 Terraform
	$(PYTHON) -m ruff check --fix src tests
	$(PYTHON) -m ruff format src tests
	@$(MAKE) --no-print-directory tf-fmt-write

e2e: ## 本地全链路演练（临时目录模拟 CPFS，不连阿里云）
	./scripts/local-e2e.sh

# 每个 make 配方行是独立的 shell，所以守卫和命令必须写在同一行里，
# 否则 `exit 0` 只退出守卫那一行，后续命令仍会执行。
tf-fmt: ## 检查 Terraform 格式
	@if [ -d infra ]; then $(TERRAFORM) fmt -check -recursive infra; \
	else echo "infra/ 尚不存在，跳过"; fi

tf-fmt-write:
	@if [ -d infra ]; then $(TERRAFORM) fmt -recursive infra; fi

tf-validate: ## 逐个 Terraform 目录 init -backend=false + validate
	@if [ ! -d infra ]; then echo "infra/ 尚不存在，跳过"; exit 0; fi; \
	set -eu; \
	for dir in $$(find infra -name '*.tf' -exec dirname {} \; | sort -u); do \
		echo "==> $$dir"; \
		$(TERRAFORM) -chdir=$$dir init -backend=false -input=false -no-color >/dev/null; \
		$(TERRAFORM) -chdir=$$dir validate -no-color; \
	done

hooks: ## 对全仓库跑一遍 pre-commit
	pre-commit run --all-files

discover: ## 只读探测阿里云资源 ID，输出 import 与 tfvars 草稿
	./scripts/discover-aliyun-ids.sh

render-ram: ## 从 Terraform 重新渲染 deploy/ram/*.json
	./scripts/render-ram-policies.sh
