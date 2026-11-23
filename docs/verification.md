# 验证记录

> 记录「这个项目到底在什么环境里跑过、结论是什么」，便于复现与判断成熟度。
> 最后更新：2026-11-24。

## 1. 单元测试（离线）

| 项 | 值 |
| --- | --- |
| 环境 | Windows 10/11，Python 3.12.10，pytest 9.2 |
| 命令 | `python -m pytest` / `ruff check src tests` |
| 结果 | **103 passed**、ruff 无告警 |
| 覆盖 | 模拟飞牛库解析（直播/电影/电视剧/特别篇/合并季噪声）、比例判定、区间映射、幂等、
远端收藏核对、鉴权失败、限速重试、Bangumi 请求构造、CLI 命令与退出码 |

## 2. 真实媒体库（飞牛 NAS）

| 项 | 值 |
| --- | --- |
| 系统 | fnOS **1.2.1102**，内核 `6.18.18.c1186-trim`，AMD FX-7500 |
| 数据库 | `trimmedia.db`（`root:root 0700`，journal=delete，约 7.4 MB） |
| 数据 | 动漫库 **13 季 / 296 集 / 4 个账号**；电视剧与电影被路径过滤（141 集） |
| 快照 | 容器内只读连接 `VACUUM INTO` → `/data/snapshot/origin.db`，`quick_check` 通过 |
| 命令 | `docker compose run --rm bangumi-syncer --config /config/config.toml dump` |

## 3. 容器（飞牛 NAS 实机）

| 项 | 值 |
| --- | --- |
| Docker | 28.5.2，Compose v2.40.3 |
| 构建 | `BASE_IMAGE=docker.fnnas.com/library/python:3.12-slim docker compose build`（`.env` 指定） |
| 镜像 | `ghcr.io/wwb051221/bangumi-syncer:latest`，**120 MB**（amd64） |
| 运行 | `docker compose run --rm ... run --once`，dry-run，无需令牌 |
| 状态页 | `/healthz` → `{"ok": true}`；`/api/status` 正常；首页 4 KB |
| 网络 | 容器内直连 `api.bgm.tv` 成功（搜索 + 章节列表） |
| 匹配 | 药屋少女的呢喃 → **420628**（0.97）；魔卡少女樱 → **1940**（1.00） |
| 耗时 | 2 部番、约 30 次请求，**24.4 秒**（默认 1 秒/请求限速） |
| 合并季 | 正确提示 `local episode 29 maps to #29 ... need split mapping` |
| 收藏状态 | dry-run 下记录 `would set collection: watching`，未写远端 |

## 4. 未验证 / 已知限制

| 项 | 说明 |
| --- | --- |
| **真正写入 Bangumi** | 需要用户在 <https://bgm.tv/dev/apps> 自建应用并授权，仓库不含任何令牌；代码路径已由假 tracker + HTTP 替身测试覆盖 |
| OAuth 回调 | 本地回调与 `--manual` 两种模式均有实现，但未用真实 client_id 走通（同上） |
| arm64 镜像 | 本机与 NAS 均为 amd64；CI 的 release 工作流会构建 `linux/amd64,linux/arm64`，未在真机验证 |
| 飞牛版本差异 | 仅 1.2.1102 实测；schema 探测 + 可覆盖 SQL 用于兜底 |
| Windows 容器 | 不支持（镜像基于 Linux） |

## 5. 复现步骤

```bash
# 本机：离线测试
pip install -e ".[dev]" && python -m pytest && ruff check src tests

# NAS：构建与演练（dry-run 不需要令牌）
cd /vol1/1000/docker/bangumi-syncer
echo 'BASE_IMAGE=docker.fnnas.com/library/python:3.12-slim' > .env
docker compose build
docker compose run --rm bangumi-syncer --version
docker compose run --rm bangumi-syncer --config /config/config.toml dump
docker compose run --rm bangumi-syncer --config /config/config.toml run --once
```
