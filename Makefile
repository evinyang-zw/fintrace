.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
AGENT ?= null
JUDGE ?= deterministic
ARGS ?=
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

eval: ## 跑评测（AGENT=react JUDGE=deterministic ARGS="--limit 3"）
	$(UV) run python -m eval.runner --agent $(AGENT) --judge $(JUDGE) $(ARGS)

embed-smoke: ## 百炼 embedding 连通性验证（不写库）
	$(UV) run python -m pipelines.embed --smoke

rerank-smoke: ## 百炼 rerank 连通性验证（不写库）
	PYTHONPATH=. $(UV) run python scripts/rerank_smoke.py

embed: ## 向量回填（需 DASHSCOPE_API_KEY）
	$(UV) run python -m pipelines.embed

ablate: ## 三档消融：bm25 / hybrid / hybrid_rerank，各 2 个 seed
	bash scripts/ablate.sh

annotation: ## 生成 60 条判官盲标包（data/annotation/labels.csv）
	$(UV) run python scripts/annotation_pack.py

kappa: ## 用人工标签计算加权 κ（需先填 labels.csv）
	PYTHONPATH=. $(UV) run python scripts/kappa.py

hedge-bench: ## 用本地假服务验证对冲请求（10s 慢调用 -> 1s）
	PYTHONPATH=. $(UV) run python scripts/hedge_bench.py

citation-annotation: ## 生成 30 条引用 claim 盲标包
	PYTHONPATH=. $(UV) run python scripts/citation_pack.py

citation-kappa: ## 引用校验器的人工 κ（需先填 citation_labels.csv）
	PYTHONPATH=. $(UV) run python scripts/citation_kappa.py

ask: ## 问一个临时问题：make ask Q="苹果 FY2024 营收？" UNIVERSE=AAPL
	$(UV) run python -m agent.cli "$(Q)" --universe "$${UNIVERSE:-AAPL}" $(ARGS)

graph-ask: ## 走图版：make graph-ask Q="..." UNIVERSE=AAPL [ARGS="--review"]
	$(UV) run python -m agent.graph_cli ask "$(Q)" --universe "$${UNIVERSE:-AAPL}" $(ARGS)

resume: ## 恢复中断的图：make resume THREAD=... ACTION=approve
	$(UV) run python -m agent.graph_cli resume "$(THREAD)" --action "$${ACTION:-approve}" --text "$${TEXT:-}"

ingest-fixtures: ## 对抗语料入库（A1 题的前置）
	$(UV) run python -m pipelines.fixtures

compare: ## 配对比较两份报告：make compare A=reports/a.json B=reports/b.json
	$(UV) run python scripts/compare_agents.py "$(A)" "$(B)" $(ARGS)

compare-agents: ## compare 的别名（与 W4 计划里的命令名一致）
	$(UV) run python scripts/compare_agents.py "$(A)" "$(B)" $(ARGS)

trace: ## 打开 trace 回看页（Streamlit）
	$(UV) run streamlit run web/trace_app.py

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

.PHONY: help install up down psql universe stats ingest ingest-fixtures q-check eval ask graph-ask resume compare compare-agents trace test lint fmt type check pricing embed embed-smoke rerank-smoke ablate annotation kappa hedge-bench citation-annotation citation-kappa
