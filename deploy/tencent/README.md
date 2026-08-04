# 思衡 V6：腾讯云 Lighthouse 部署资产

这是一个**独立、单机、教师演示用**的部署骨架。它只启动 V6 的 FastAPI、Vue/Nginx 和独立 SQLite 命名卷；不会接管共享 Caddy、DNS、证书、防火墙、其他容器或任何旧版测评数据。

当前构成：

```text
shared Caddy `tencent-caddy-1` (80/443, HTTPS)
                  |  external Docker network: tencent_default
                  v
           cta-v6-web:8080 (V6 frontend / Nginx) -> backend:8060 -> DeepSeek
                  |                                         |
                  +-- 127.0.0.1:18060 (host-only probe)      v
                                           cta-v6-sqlite-data named volume
```

`18060` 只绑定到 `127.0.0.1`，因此不会被公网直接访问；公网流量必须经现有共享 Caddy 的 443 转入外部 Docker 网络 `tencent_default`，再访问 V6 专用别名 `cta-v6-web:8080`。访谈入口不要求用户名或密码；`/admin` 与 `/api/v1/admin` 单独要求管理员凭据，健康检查不向公网提供。DeepSeek Key 仅注入后端容器。

## 服务器前提

- 一台已确认可用于新增 V6 容器的 Tencent Lighthouse Linux 主机，Docker Engine 与 Docker Compose v2 已可用。
- `thinkagent.asia` 的 A 记录已指向这台服务器；此域名原有的 V5 路由已退役，可替换为 V6。不要改动其他已有站点域名。
- 现有共享 Caddy 容器为 `tencent-caddy-1`，并已加入 `tencent_default`。发布者只能安全地添加一个**新** host block，不能替换全局配置、容器或网络。
- 服务器有外网访问 `https://api.deepseek.com` 的能力；公网只开放 Caddy 所需的 `80/443`，不要开放 `18060`。

## 首次准备

在服务器上放置不含本地 `.env`、数据库或 `node_modules` 的 V6 源码，然后在工程根目录执行：

```sh
cp .env.production.example .env.production
chmod 600 .env.production
```

仅在服务器上的 `.env.production` 填入：

- `DEEPSEEK_API_KEY`：真实模型 Key；不得复制到前端、Git、终端回显、截图或聊天记录。
- `ADMIN_TOKEN`：可用 `openssl rand -hex 32` 生成的 64 位十六进制值。
- `SITE_BASIC_USER` 与 `SITE_BASIC_PASSWORD`：只用于 `https://thinkagent.asia/admin` 的浏览器认证；不影响公开访谈入口，密码至少 16 位且不应复用日常账户密码。

不要在服务器仓库内创建 `backend/.env`；生产 Compose 只读取工程根的 `.env.production`，并将 DeepSeek Key 传给后端容器，不传给前端容器。

## 构建与本机健康检查

```sh
./deploy/tencent/deploy.sh
curl -fsS http://127.0.0.1:18060/healthz
./deploy/tencent/backup-sqlite.sh
```

脚本会构建并启动名为 `cta-v6` 的独立 Compose 项目，等待 Nginx 和数据库迁移健康；它不会修改 Caddy、DNS、TLS 或防火墙。SQLite 数据保存在 Docker 命名卷 `cta-v6-sqlite-data` 中，备份脚本使用 SQLite 在线备份 API，在工程的 Git 忽略目录 `backups/` 写入权限为 `0600` 的快照，不会删除任何旧备份。

停止或查看这一个项目时，始终带上此 Compose 文件，避免误操作共享容器：

```sh
docker compose --env-file .env.production -f docker-compose.production.yml ps
docker compose --env-file .env.production -f docker-compose.production.yml logs --tail=100 backend frontend
docker compose --env-file .env.production -f docker-compose.production.yml down
```

最后一条只停止 V6 容器，**不加 `-v`**，因此不会删除 `cta-v6-sqlite-data`。

## 接入共享 Caddy（单独变更）

先备份共享 Caddy 的既有配置源，再用 [Caddyfile.thinkagent.asia](Caddyfile.thinkagent.asia) 的**完整单个 host block**替换其中仅有的 `thinkagent.asia` 旧站点块；不要覆盖整个 Caddyfile、容器、网络或其他站点路由。该片段将 `thinkagent.asia` 反代到 `cta-v6-web:8080`，并在公网隐藏 `/healthz`。

合并但尚未 reload 后，运行：

```sh
./deploy/tencent/deploy.sh reload-caddy
```

它会在 `tencent-caddy-1` 内先确认当前配置确实含 V6 hostname 与 `cta-v6-web:8080` 路由，再执行 `caddy validate` 和平滑 `caddy reload`。脚本不复制、重写或替换任何共享 Caddy 文件。若该容器或 Caddyfile 路径在服务器上不同，只能显式覆盖 `CTA_V6_CADDY_CONTAINER` 或 `CTA_V6_CADDYFILE_PATH` 后再执行。

在 Caddy 与 DNS 都生效后，使用 HTTPS 验证：`/assessment`、模型生成开场、一次追问、用户主动完成、报告和 PDF。`https://thinkagent.asia/admin` 应要求管理员认证，认证后可加载复核台；`https://thinkagent.asia/healthz` 与 API health 应为 `404`；服务器本机 `http://127.0.0.1:18060/healthz` 应为 `200`。

## 发布边界

- 这是探索性、非标准化 Demo；不得声称正式测评效度、跨用户可比性或对教师演示之外的生产适用性。
- `.env.production` 内的真实值、共享 Caddy 的实际源文件位置和 DNS 状态均应在服务器上再次核对；不要把这些内容复制回仓库或聊天记录。
- 真实 DeepSeek、浏览器麦克风、TTS、断网恢复以及公网 HTTPS 需要各自留存验收证据；容器健康不等于这些体验已经验收。
