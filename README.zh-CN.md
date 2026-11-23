# Bangumi-syncer

> 把 **飞牛影视（fnOS `trim.media`）** 的动漫观看进度自动同步到 **Bangumi 番组计划**。
> 多用户、只读数据源、默认演练、可人工复核 —— 一个零依赖的容器。

[![CI](https://github.com/wwb051221/Bangumi-syncer/actions/workflows/ci.yml/badge.svg)](https://github.com/wwb051221/Bangumi-syncer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Image size](https://img.shields.io/badge/image-~55MB-informational.svg)](#docker)

**English**: [README.md](README.md) · **设计文档**: [架构](docs/architecture.md) · [匹配算法](docs/mapping.md) · [飞牛数据库结构](docs/schema.md)

---

## 它解决什么问题

飞牛影视自己记录「看到第几集、看到第几分钟」，Bangumi 记录「这集的收藏状态」。
两边没有互联，追番党只能手动点。本项目在中间做定期同步：

```
┌─────────────────────┐        ┌──────────────────┐        ┌─────────────────┐
│ 飞牛影视 trim.media │        │  Bangumi-syncer  │        │   api.bgm.tv    │
│                     │  只读  │                  │  HTTPS │                 │
│ trimmedia.db ───────┼───────▶│ 快照 → 匹配 →    │───────▶│ 章节收藏 / 收藏 │
│  (root 700, 关闭)   │  VACUUM│ 幂等推送          │  OAuth │  状态           │
└─────────────────────┘  INTO  └──────────────────┘        └─────────────────┘
         ▲                               │
         │ 轮询（默认 5 分钟）            ▼
   播放进度/看完标记              状态页 :8099（只读）
```

## 特性

| 特性 | 说明 |
| --- | --- |
| **只做动漫** | 按媒体库路径过滤（如 `/vol1/1000/影片/动漫`），电视剧/电影/直播流一律忽略 |
| **多用户** | 每个飞牛账号独立计算进度，可映射到各自的 Bangumi 账号，也可多点一 |
| **绝不写坏源库** | 对 `trimmedia.db` 只做只读 `VACUUM INTO` 一致性快照，不锁库、不产生 `-wal` |
| **默认演练** | `app.dry_run = true` 时只记录「本应推送什么」，确认无误后再开真写 |
| **幂等** | 已推送的集不会重复推送；可选再向 Bangumi 核对已有收藏 |
| **可复核** | 匹配置信度不足时不猜，塞进复核队列，用 `review` / `map add` 一键确认 |
| **合并季支持** | 一季文件夹里塞了 55 集（多个 Bangumi 条目）时，用区间映射拆成 3 段 |
| **抗 schema 变化** | 自动探测必备列，缺列时给出明确报错；SQL 可在配置里替换，无需改代码 |
| **零依赖** | 只用 Python 标准库，镜像 ~55 MB，内存占用 < 30 MB |
| **可观测** | JSON 日志、`/healthz`、`/api/status`、只读状态页 |

## 实测环境

| 项 | 值 |
| --- | --- |
| 系统 | fnOS **1.2.1102**（x86_64） |
| 应用 | `trim.media`（飞牛影视，系统级应用） |
| 数据库 | `/usr/local/apps/@appdata/trim.media/database/trimmedia.db`，`root:root 0700`，`journal_mode=delete` |
| 样本 | 动漫库 13 季 / 296 集，自动匹配 10 季正确，3 季进入人工复核（Bangumi 无独立条目） |
| Bangumi | `api.bgm.tv` v0 接口，直连正常，无需代理 |

## 快速开始（Docker Compose，推荐）

```bash
git clone https://github.com/wwb051221/Bangumi-syncer.git
cd Bangumi-syncer
cp config.example.toml config.toml
```

**1. 建一个 Bangumi 应用**：<https://bgm.tv/dev/apps> → 新建，回调地址填
`http://127.0.0.1:8765/oauth/callback`，记下 `App ID` / `App Secret`。

**2. 填配置**：编辑 `config.toml` 里的
`[[target.accounts]]`（`client_id` / `client_secret`）和 `[[users]]`（飞牛用户名 → Bangumi 账号）。
敏感值也可以放环境变量，配置里写 `${BGM_CLIENT_ID}`。

**3. 授权**（令牌会存进 `/data/state/state.db`，7 天自动续期）：

```bash
docker compose run --rm bangumi-syncer oauth --account main --manual
```

**4. 先演练一轮**（`config.toml` 里保持 `dry_run = true`）：

```bash
docker compose run --rm bangumi-syncer run --once
docker compose logs --tail=100 bangumi-syncer
```

> 演练模式只调用 Bangumi 的公开接口（搜索、章节列表），**不需要先授权**，
> 可以先看匹配结果再决定是否 oauth。

确认日志里的匹配与集数都对，再：

```bash
sed -i 's/^dry_run = true/dry_run = false/' config.toml
docker compose up -d
docker compose logs -f
```

状态页：<http://飞牛IP:8099/>（只读，展示最近运行、映射表、待复核条目）。

> 没有 Docker？也可以直接跑：
> `pip install .` → `bangumi-syncer --config config.toml run`
> （需要能读到 `trimmedia.db`，即 root 或已放宽 ACL）。

## 配置说明

完整注释见 [`config.example.toml`](config.example.toml)，几个关键项：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `app.dry_run` | `true` | 演练模式，永不写 Bangumi |
| `source.mode` | `live` | `live` 自行做快照；`snapshot` 读现成快照 |
| `source.include_paths` | `[]` | **建议填动漫库路径**，否则电视剧也会进匹配 |
| `source.skip_season_zero` | `true` | 跳过特别篇（Bangumi 本篇章节不包含 SP） |
| `sync.interval_seconds` | `300` | 轮询间隔 |
| `sync.watch_ratio` | `0.9` | 进度达到片长 90% 视为看完 |
| `sync.verify_remote` | `true` | 推送前先读 Bangumi 已有收藏，避免重复 |
| `match.min_score` | `0.86` | 低于此分不自动映射 |
| `match.min_margin` | `0.06` | 前两名太接近就算有歧义 |

## 多用户

```toml
[[target.accounts]]
name = "me"
client_id = "..."
client_secret = "..."

[[target.accounts]]
name = "kids"
client_id = "..."      # 另一个 Bangumi 账号
client_secret = "..."

[[users]]
fnos_user = "wwb051221"   # 飞牛用户名（或 guid）
account = "me"

[[users]]
fnos_user = "2708"
account = "kids"
enabled = true
watch_ratio = 0.95        # 该用户单独覆盖阈值
```

```bash
bangumi-syncer oauth --account me
bangumi-syncer oauth --account kids
```

每个用户的进度完全独立；同一个 Bangumi 账号被多个飞牛用户共用时，推送结果为并集（已推送的集不会重复写）。

## 匹配与人工复核

标题匹配是启发式的（飞牛用 TMDB 标题，Bangumi 是日文原名 + 中文名，且两边**没有共享 ID**）。
程序会给每个候选算分（标题相似度 + 年份 + 集数 + 季号），只有**分数够高且明显领先第二名**才自动映射。

以实测库为例：

| 飞牛标题 | 结果 |
| --- | --- |
| 药屋少女的呢喃 S1（55 集，合并了三季） | ✅ 自动 → 420628（第一季 24 集），其余集提示拆分 |
| 魔卡少女樱 | ✅ 自动 → 1940 |
| 总之就是非常可爱 S2 | ✅ 自动 → 355798（第二季，而非第一季） |
| 茶啊二中 S1–S5 | ✅ 自动 → 148295 / 148296 / 191336 / 241442 / 501710 |
| 茶啊二中泡面番 / 男寝501 / 嘎嘎以嘎斯 | ⚠️ 进入复核（Bangumi 无对应独立条目） |

**查看待复核：**

```bash
bangumi-syncer review
```

```
茶啊二中泡面番  S1  (2016, 21 eps)
  key    fnos:2db5e6c8…:s1
  reason best candidate 0.394 < min_score 0.860
  subject  score  name_cn   name      year  eps
  148295   0.394  茶啊二中  茶啊二中   2014  10
  -> bangumi-syncer map add --account main --series 'fnos:2db5e6c8…:s1' --subject 148295
  -> for a merged season add an explicit range, e.g. --from 25 --to 48
```

**手动指定**（也可以直接按 `bgm.tv/subject/<id>` 的 id 填）：

```bash
bangumi-syncer search -k "茶啊二中"
bangumi-syncer map add --account main --series 'fnos:xxxx:s1' --subject 148295
```

**合并季拆分**（飞牛把多季放在同一个 Season 里，如 1–55 集）：

```bash
bangumi-syncer map add --account main --series 'fnos:xxxx:s1' --subject 420628 --from 1  --to 24
bangumi-syncer map add --account main --series 'fnos:xxxx:s1' --subject 486347 --from 25 --to 48
bangumi-syncer map add --account main --series 'fnos:xxxx:s1' --subject 568244 --from 49 --to 60
```

区间映射按「本地第 N 集 → 该条目的第 N-起始集+1 集」换算，所以拆分后的编号不需要对齐 Bangumi 的绝对集号。
修改映射会自动清掉该区间的推送记录，下一轮重新推送。

## 状态页与命令

```bash
bangumi-syncer run                 # 常驻循环（容器默认命令）
bangumi-syncer run --once          # 只跑一轮，适合 cron / 排障
bangumi-syncer status              # 最近运行、令牌、统计
bangumi-syncer review [--json]     # 待复核条目及候选
bangumi-syncer map list|add|remove # 映射管理
bangumi-syncer search -k 关键词     # 查 Bangumi 条目 id
bangumi-syncer dump [--json]       # 媒体服务器解析结果（不写任何东西）
bangumi-syncer oauth --account X   # 授权（--manual 适合远程 SSH）
```

全局参数：`--config PATH`、`--dry-run/--no-dry-run`、`--log-level`、`--log-format json`。

退出码：`0` 成功、`1` 运行失败、`2` 配置错误、`3` 需要重新授权。

## 安全

* **只读挂载**：`/usr/local/apps/@appdata/trim.media/database:/source:ro`，程序只会 `VACUUM INTO` 到自己的卷。
* **容器以 root 运行**是因为该数据库是 `root:700`；这是读取它的最小代价。如果在意，请改用
  `source.mode = "snapshot"` + 宿主 cron 生成快照，再把容器改成非 root（compose 里删掉 `user:`）。
* **令牌**保存在 `/data/state/state.db`（SQLite，0600 建议），不写进配置文件；配置文件可只读挂载。
* **网络出站**只有 `api.bgm.tv` / `bgm.tv`（OAuth），不做任何遥测。
* 状态页**没有写接口**，所有变更都走 CLI。

## 常见问题

<details>
<summary><b>读不到 trimmedia.db（Permission denied）</b></summary>

数据库是 `root:root 0700`。容器需要以 root 运行，或改用 snapshot 模式。
如果连 root 都读不到，检查飞牛是不是把目录搬到了别处：`bangumi-syncer dump` 会打印它尝试的路径。
</details>

<details>
<summary><b>要不要停飞牛影视再同步？</b></summary>

不用。`live` 模式用只读连接 + `VACUUM INTO`，不会锁库也不会打断播放。
</details>

<details>
<summary><b>能不能直接打开 NAS 上的数据库？</b></summary>

不建议跨网络直接打开。如果飞牛某天启用了 WAL，远端只读连接可能读到不完整状态或创建失败。
本项目始终先做本地快照再读。
</details>

<details>
<summary><b>一直显示 “no token”</b></summary>

令牌有效期 7 天、用 refresh token 自动续期；但如果你在 Bangumi 侧撤销了应用授权，就需要重跑
`bangumi-syncer oauth --account main`。日志里出现 `auth error` 时退出码为 3。
</details>

<details>
<summary><b>某部番永远匹配不上</b></summary>

很正常：Bangumi 没有这条目、或者标题差异太大（中文译名 / 剧场版 / 泡面番）。
`review` 里会保留候选；实在没有就 `map remove` 掉，让它安静待在复核队列。
</details>

<details>
<summary><b>飞牛升级后报 schema 变化</b></summary>

报错会列出缺失的表/列。先在 `dump` 里确认新结构，然后按 [docs/schema.md](docs/schema.md)
在 `[source.fnos]` 里覆盖 `episodes_query` / `plays_query` / `users_query`，不用改代码。
</details>

<details>
<summary><b>runtime 为 0 的老片怎么算看完？</b></summary>

没有时长就无法按比例判断，只认飞牛自己的 `watched = 1`。这类片源建议手动在 Bangumi 点一下。
</details>

## 开发

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest            # 103 个用例，全部离线（含模拟 fnOS 库）
ruff check src tests
```

目录：

```
src/bangumi_syncer/
  config.py        配置解析（TOML + ${ENV} 展开）
  models.py        数据模型与“看完”判定
  sources/fnos.py  飞牛数据库适配（快照、schema 探测、路径过滤）
  trackers/bangumi.py  Bangumi v0 API（OAuth、限速、重试、幂等）
  matcher.py       标题匹配评分
  sync.py          同步引擎（多用户、映射、推送、收藏状态）
  state.py         SQLite 状态（令牌/映射/推送/候选/运行）
  server.py        只读状态页 + JSON
  cli.py           命令行
tests/             103 个离线用例（真实 schema 的模拟库 + 假 tracker）
docs/              架构、匹配算法、飞牛库结构
```

想接入新的媒体服务器（Jellyfin / Emby / Plex）？实现 `sources/base.py` 的 `SourceAdapter`
并在 `sources/__init__.py` 注册即可，其余逻辑完全复用。详见 [docs/architecture.md](docs/architecture.md)。

## 路线图

- [ ] Jellyfin / Emby / Plex 数据源适配器
- [ ] 剧场版（Movie）→ Bangumi 条目「看过」
- [ ] 状态页上的「确认映射」按钮（带 CSRF 的写接口）
- [ ] `--dry-run` 时输出 diff 报告文件
- [ ] 多 Bangumi 账号同时写同一用户（主备）
- [ ] AniList / MAL 目标适配器

## 许可

[MIT](LICENSE)。与飞牛、Bangumi 均无隶属关系；Bangumi API 请遵守其使用条款，保持默认 1 秒限速。
