# 运行手册（W5）

面向"半夜出事、第二天要面试"的场景。所有命令都在服务器上执行，除非写明"本地"。

服务器：`47.96.253.123`（阿里云 2C4G / Ubuntu 24.04）· 目录 `/opt/fintrace` · 单实例。

---

## 0. 一分钟判断"是不是挂了"

```bash
cd /opt/fintrace
docker compose --env-file .env -f deploy/compose.prod.yml ps
curl -s localhost/api/health | python3 -m json.tool
```

`/api/health` 返回 `ok:true` 且 `db:true` 才算健康；`queue_depth` 持续接近 `queue_capacity` 说明在排队，不是挂了。

---

## 1. 部署一个新版本

正常路径：push 到 `main` → GitHub Actions `release` 构建、扫描、推 GHCR → **`production` 环境人工批准** → 服务器拉镜像、重启、健康检查。

手动部署（GHCR 拉取太慢时用）：

```bash
# 本地：离线打包镜像（必须是 linux/amd64）
docker build --platform linux/amd64 -f deploy/Dockerfile -t fintrace:prod .
docker save fintrace:prod | gzip -1 > /tmp/fintrace-image.tar.gz
scp /tmp/fintrace-image.tar.gz root@47.96.253.123:/opt/fintrace/image.tar.gz

# 服务器
cd /opt/fintrace
gunzip -c image.tar.gz | docker load
docker tag fintrace:prod ghcr.io/evinyang-zw/fintrace:manual
export IMAGE=ghcr.io/evinyang-zw/fintrace:manual
docker compose --env-file .env -f deploy/compose.prod.yml up -d app mcp --remove-orphans
curl -fsS localhost/api/health
```

> **架构陷阱**：Mac 默认构建 arm64，服务器是 amd64，加载后报
> `exec format error`。所有手动构建都必须带 `--platform linux/amd64`。

---

## 2. 回滚

```bash
cd /opt/fintrace
export IMAGE=ghcr.io/evinyang-zw/fintrace:<上一个可用 sha>
docker compose --env-file .env -f deploy/compose.prod.yml pull app mcp
docker compose --env-file .env -f deploy/compose.prod.yml up -d app mcp
curl -fsS localhost/api/health
```

数据库向后兼容：新表/新列都是 `CREATE TABLE IF NOT EXISTS`，回滚镜像不用回滚数据。

---

## 3. 重启与中断

- 重启 app：在飞的 run 会被标记为 `failed`（原因 `interrupted by restart`），不会永远 `running`。
- 人工待审的 run 存在 `runs.status = awaiting_review`，重启后可继续 `POST /api/runs/{id}/review`。
- 只重启工具层：

```bash
docker compose --env-file .env -f deploy/compose.prod.yml restart mcp
```

---

## 4. 数据库备份与恢复

```bash
# 备份（本地或服务器）
docker exec fintrace-prod-db-1 pg_dump -U fintrace -Fc fintrace > warehouse-$(date +%F).dump

# 恢复（会覆盖现有数据！先停 app）
docker compose --env-file .env -f deploy/compose.prod.yml stop app
docker exec -i fintrace-prod-db-1 pg_restore -U fintrace -d fintrace --no-owner --clean --if-exists < warehouse.dump
docker compose --env-file .env -f deploy/compose.prod.yml start app
```

完整重建（没有 dump 时）：`make ingest && make embed`（约 20 分钟，需要 SEC / 百炼的 key）。

---

## 5. 密钥轮换

1. 改 `/opt/fintrace/.env`（`chmod 600`）
2. `docker compose ... up -d app mcp`（容器会重建并读到新值）
3. GitHub 侧只存部署用的 `SSH_*`；LLM key 不上 GitHub。

`API_TOKEN` / `DEMO_TOKEN` 轮换后，旧 token 立即失效；页面需要重新输入。

---

## 6. 成本失控 / 被打

- 日预算在 `.env` 的 `API_DAILY_BUDGET_USD`，超了 `/api/ask` 返回 503。
- 紧急止血：

```bash
# 把 demo token 清空并重启
sed -i 's/^DEMO_TOKEN=.*/DEMO_TOKEN=/' .env
docker compose --env-file .env -f deploy/compose.prod.yml up -d app
```

- 看谁在打：`docker logs --tail=200 app | grep "/api/ask"`（限流按 IP，日志里有 status）。

---

## 7. 常见故障

| 现象 | 先查 | 处理 |
|---|---|---|
| app 一直 `starting` | `docker logs app` | 多为 DB 密码不对或 checkpointer 初始化失败 |
| `/api/health` `db:false` | `docker ps`、`pg_isready` | 重启 db；确认 `.env` 的 `POSTGRES_PASSWORD` 与卷初始化时一致 |
| 每题都拒答且提到工具错误 | `docker logs mcp` | 看是否 `421 Invalid Host header`（`MCP_ALLOWED_HOSTS` 少了容器名）或 DB 只读连接失败 |
| 每题都很慢 | `/dashboard` 的 p95 | provider 抖动；看 trace 里 `hedged/hedge_won`，不要先改代码 |
| GHCR 拉取超时 | `docker pull` 手动重试 | 慢是常态，用第 1 节的手动镜像传输 |
| Docker Hub 拉不到 base image | `docker pull` | 用 `docker.m.daocloud.io/...` 再 `docker tag` 回原名 |

---

## 8. 已知边界（不要假装没有）

- **单实例、`--workers 1`**：队列、SSE、限流都在进程内。扩到多实例前先换 Redis 队列。
- **GHCR 在服务器上很慢**：自动部署可能超时；手动传输是兜底。
- **没有域名 / HTTPS**：当前 `http://47.96.253.123`。上线 Caddy profile 再说。
- **p95 由 provider 决定**：W4 已证明，别再"优化"超时。
- **demo token 是公开的**：只靠每日配额与预算兜底，别给它高权限。
