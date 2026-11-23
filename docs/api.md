# HTTP API

Web 界面与脚本共用同一套 JSON 接口，默认监听 `0.0.0.0:8099`（`[server] listen`）。

## 鉴权与约定

* **没有登录**：只应在局域网使用；需要远程访问时把 `server.listen` 改为 `127.0.0.1:8099` 再走 SSH 隧道。
* **读接口**无需额外请求头。
* **写接口**（POST/DELETE）必须带：

  ```
  X-Requested-With: bangumi-syncer
  ```

  否则返回 `403`。浏览器跨站表单无法设置自定义头，服务端也不返回 CORS 头，因此可挡住 CSRF。
* 请求/响应均为 `application/json; charset=utf-8`；错误统一为 `{"error": "..."}`。
* 时间字段除特别说明外都是 **epoch 秒**；显示用的字符串字段以 `_human` 结尾。

## 读接口

### `GET /healthz`

```json
{"ok": true, "version": "0.1.0"}
```

### `GET /api/status`

界面渲染所需的全部数据：

| 字段 | 说明 |
| --- | --- |
| `version` / `dry_run` / `timezone` | 基本信息 |
| `uptime_seconds` / `next_run_at` / `next_run_at_human` | 循环状态 |
| `run_requested` | 是否已排队一次手动同步 |
| `source` | 数据源类型、模式、`stats`（季/集/用户/过滤数） |
| `users` | 配置里的飞牛用户 → 账号 |
| `accounts` | 账号令牌状态：`has_token`、`expires_at`、`remote_user` |
| `stats` | 映射数、推送数、失败数、候选数 |
| `runs` | 最近 10 次运行（含 `started_at_human`、`duration`） |
| `mappings` | 映射表（含 `subject_url`、已推送计数） |
| `review` | 待复核条目（候选 + 原因） |
| `last_result` | 本进程内最近一次运行的详细结果 |
| `logs` | 最近 200 行日志 |
| `config` | 打码后的运行配置（`client_secret` → `***`） |

### `GET /api/logs?limit=200`

```json
{"lines": ["01:08:45 INFO  bangumi_syncer.sync ..."], "run_requested": false}
```

`limit` 取值 1–1000，默认 200。

### `GET /api/search?q=关键词&limit=8&account=main`

代理 Bangumi 的公开条目搜索（不需要令牌）。

```json
{"query": "药屋少女的呢喃", "results": [
  {"subject_id": 420628, "name": "薬屋のひとりごと", "name_cn": "药屋少女的呢喃", "year": 2023, "eps": 24}
]}
```

Bangumi 侧失败时返回 `502`。

## 写接口

### `POST /api/run`

请求常驻循环立刻跑一轮（最多 0.5 秒内被拾取）。

```json
202 {"queued": true}
409 {"error": "a sync run is already queued"}
```

### `POST /api/map`

创建或覆盖映射（等价于 `bangumi-syncer map add`）。

```json
{
  "account": "main",
  "series_key": "fnos:adcd8af1...:s1",
  "subject_id": 486347,
  "ep_from": 25,
  "ep_to": 48,
  "name": "药屋少女的呢喃 第二季"
}
```

`ep_from`/`ep_to` 可省略（默认 1–9999）；`name` 省略时会尝试从 Bangumi 取。成功返回：

```json
200 {"ok": true, "subject_id": 486347, "ep_from": 25, "ep_to": 48,
     "subject_name": "药屋少女的呢喃 第二季", "cleared_pushes": 0}
```

副作用：删除该区间的推送记录（下一轮重新推送）、删除该条目的复核候选。

### `DELETE /api/map?account=main&series_key=...&ep_from=25`

删除映射。`ep_from` 省略时删除该条目全部映射与推送记录；给出时只处理对应区间。

```json
200 {"ok": true, "removed": 1, "cleared_pushes": 24}
```

### `DELETE /api/candidates?account=main&series_key=...`

只从待复核队列移除（不创建映射），之后若仍匹配失败会再次出现。

```json
200 {"ok": true}
```

## 设置接口

运行时设置保存在状态库（`meta.runtime_settings`），修改后**下一轮同步生效**，无需重启容器。

### `GET /api/settings`

```json
{
  "values": {
    "dry_run": true, "interval_seconds": 300, "watch_ratio": 0.9,
    "min_position_seconds": 60, "verify_remote": true,
    "mark_in_progress": true, "mark_completed": true,
    "include_paths": ["/vol1/1000/影片/动漫"], "exclude_paths": [],
    "skip_season_zero": true, "min_episodes": 1,
    "match": {"min_score": 0.86, "min_margin": 0.06, "search_limit": 8},
    "users": [{"fnos_user": "wwb051221", "account": "main", "enabled": true, "watch_ratio": null}]
  },
  "accounts": ["main"],
  "source": {"users": [{"key": "...", "name": "wwb051221", "plays": 3, "finished": 0}],
             "seasons": 13, "episodes": 296, "include_paths": ["/vol1/1000/影片/动漫"]},
  "source_error": null
}
```

`source` 是直接从飞牛影视数据库读到的**真实用户列表**（缓存 60 秒），失败时 `source_error` 给出原因。

### `PUT /api/settings`

部分更新（未提供的键保持当前值；`match` 对象按字段合并）。校验失败返回 `400` 与具体字段名。

```json
{
  "dry_run": false,
  "interval_seconds": 120,
  "users": [{"fnos_user": "wwb051221", "account": "main", "enabled": true, "watch_ratio": 0.95}]
}
```

约束：`interval_seconds` 30–86400、`watch_ratio` 0.05–1、`min_position_seconds` 0–3600、
`min_episodes` 1–1000、`match.min_score`/`min_margin` 0–1、`match.search_limit` 1–20、
`users` 至少一条且账号必须存在。

### `POST /api/settings/reset`

清除运行时覆盖层，恢复为 `config.toml` 的值。

```json
200 {"ok": true, "values": {...}, "accounts": ["main"]}
```

### `GET /api/source-users?refresh=1`

返回媒体库用户列表（`refresh=1` 跳过 60 秒缓存）。与 `/api/settings` 里的 `source` 结构相同。

## 外观接口（服务器端保存）

外观配置保存在状态库 `meta.ui_prefs`，背景图片保存在 `<state 目录>/assets/background.bin`，
**所有设备共享**。浏览器只用 localStorage 做首屏缓存，避免闪白。

### `GET /api/ui`

```json
{
  "theme": "auto", "accent": "#7db3ff", "font_scale": 1.0, "density": "comfortable",
  "background": {"mode": "image", "preset": "mist", "opacity": 0.55, "blur": 8, "dim": 0.45,
                 "version": 3, "content_type": "image/png", "url": "/static/background?v=3",
                 "image_bytes": 87563},
  "palette": ["#a9bccb", "#c8b9b2"], "rev": 7
}
```

`background.mode` 为 `none` / `preset` / `image`；`preset` 依赖浏览器端渐变，`image` 的 `url` 由服务端提供。
`rev` 每次修改递增，供前端判断是否需要同步。

### `PUT /api/ui`

部分更新，未提供的字段保持不变；枚举非法返回 `400`，数值越界会被夹到合法区间。

```json
{"theme": "dark", "accent": "#c792ea", "font_scale": 1.1, "density": "compact",
 "background": {"opacity": 0.4}}
```

### `PUT /api/ui/background`

三种写法：

* **上传图片**（推荐）：请求体是二进制，`Content-Type: image/png|jpeg|webp|gif`，≤ 8 MB；
* **图片 URL**：`{"url": "https://example.com/a.jpg"}`，服务端下载后保存（避免跨域/图床失效）；
* **内置预设**：`{"preset": "mist"}`，可选 `mist` / `dusk` / `ocean` / `paper`。

```json
200 {"ok": true, "ui": {...}}
```

### `DELETE /api/ui/background`

删除图片文件并把 `mode` 设为 `none`。

### `PUT /api/ui/palette`

保存从背景提取的莫兰迪配色（最多 8 个 `#rrggbb`）：

```json
{"colors": ["#a9bccb", "#c8b9b2", "#b5a8b8"]}
```

### `GET /static/background`

返回当前背景图片（`Cache-Control: public, max-age=3600` + `ETag`）；没有图片时 404。

## 示例

```bash
# 触发一次同步
curl -s -X POST -H 'X-Requested-With: bangumi-syncer' http://nas:8099/api/run

# 搜索条目
curl -s 'http://nas:8099/api/search?q=%E8%8D%AF%E5%B1%8B%E5%B0%91%E5%A5%B3%E7%9A%84%E5%91%A2%E5%96%83'

# 合并季拆分：25–48 集映射到第二季
curl -s -X POST -H 'Content-Type: application/json' -H 'X-Requested-With: bangumi-syncer' \
  -d '{"account":"main","series_key":"fnos:xxxx:s1","subject_id":486347,"ep_from":25,"ep_to":48}' \
  http://nas:8099/api/map
```
