.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
DATABASE_URL ?= postgresql://fintrace:fintrace@localhost:5433/fintrace
export DATABASE_URL

help: ## 显示可用命令
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## 安装/同步依赖
	$(UV) sync

up: ## 启动 Postgres（pgvector）
	docker compose up -d --wait db

down: ## 停止并移除容器
	docker compose down

psql: ## 进入 psql
	docker compose exec db psql -U fintrace -d fintrace

universe: ## 解析 ticker -> CIK，写 universe.lock.json
	$(UV) run python -m pipelines.universe

stats: ## 查看仓库存量（公司/filing/facts/覆盖率）
	$(UV) run python -m pipelines.stats

ingest: ## 抓取财报并入库
	$(UV) run python -m pipelines.ingest

q-check: ## 校验评测集（schema + 数值对账）
	$(UV) run python -m eval.check

eval: ## 跑评测（默认 null agent）
	$(UV) run python -m eval.runner --agent null

test: ## 跑单元测试（不含 live）
	$(UV) run pytest -m "not live"

lint: ## 静态检查
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## 自动格式化
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

type: ## 类型检查
	$(UV) run mypy

check: lint type test ## 提交前全量检查

pricing: ## 校验单价表（峰谷/缓存规则 + served model 对齐）
	$(UV) run python -m agent.pricing

.PHONY: help install up down psql universe stats ingest q-check eval test lint fmt type check pricing
