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

## 登录与用户接口

首次启动（状态库中没有用户）时，除登录页静态资源外所有 API 返回 `401 {"error":"setup required"}`，
只能调用 `POST /api/auth/setup`。初始化完成后，未登录请求返回 `401 {"error":"unauthorized"}`，
登录后使用 `bs_session` Cookie（HttpOnly、SameSite=Lax、7 天有效）。所有写请求仍需
`X-Requested-With: bangumi-syncer`。

### `GET /api/auth`

```json
{"setup_required": false, "authenticated": true, "username": "owner", "role": "admin"}
```

### `POST /api/auth/setup`

仅在没有用户时可用，创建第一个管理员并直接登录：

```json
{"username": "owner", "password": "secret123", "confirm": "secret123"}
```

### `POST /api/auth/login` / `POST /api/auth/logout`

```json
{"username": "owner", "password": "secret123"}
→ 200 {"ok": true, "auth": {...}} + Set-Cookie: bs_session=...
```

失败返回 `401`。登出清空 Cookie。

### `POST /api/auth/password`

修改当前登录用户的密码：

```json
{"current_password": "…", "new_password": "…", "confirm": "…"}
```

### 用户管理（仅管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/users` | `{"users": [{"username","role","created_at"}]}` |
| POST | `/api/users` | `{"username","password","role":"member\|admin"}` |
| POST | `/api/users/reset` | `{"username","password"}`，也可带 `role` 改角色 |
| DELETE | `/api/users?name=kid` | 删除用户（不能删除自己或最后一个管理员） |

非管理员访问 `/api/users*` 返回 `403`；删除用户后其现有 Cookie 立即失效。

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
             "seasons": 13, "episodes": 296, "include_paths": ["/vol1/1000/影片/动漫"],
             "folders": {"roots": [{"path": "/vol1/1000/影片", "name": "影片",
                         "series": 17, "episodes": 437,
                         "children": [{"path": "/vol1/1000/影片/动漫", "name": "动漫",
                                       "series": 13, "episodes": 296, "children": []}]}],
                         "series": 17, "episodes": 437}},
  "source_error": null
}
```

`source` 是直接从飞牛影视数据库读到的**真实用户列表**（缓存 60 秒），失败时 `source_error` 给出原因。

`source.folders` 是给设置页目录选择器用的目录树：由剧集文件路径聚合而来，单链目录会被折叠，
每个节点带 `series`（季数）与 `episodes`（集数），`roots` 可有多棵。目录树使用**未过滤**的全量
快照，因此当前被排除的库也能重新勾选；读取失败时会退化为已过滤视图。

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

返回媒体库用户列表与目录树（`refresh=1` 跳过 60 秒缓存）。与 `/api/settings` 里的 `source` 结构相同。

## Bangumi 授权接口

网页端填写 OAuth 应用凭据并完成授权，凭据存入状态库 `meta.oauth_clients`（按账号）。
**App Secret 只写不读**：任何响应都不会返回它。

### `GET /api/oauth`

```json
{"accounts": [{
  "name": "main", "client_id": "12345", "has_secret": true,
  "redirect_uri": "http://127.0.0.1:8765/oauth/callback",
  "has_token": false, "expires_at": null, "remote_user": null
}]}
```

`client_id` 来自网页保存值或 `config.toml`/环境变量（后者作为回退）。

### `PUT /api/oauth`

保存某个账号的 OAuth 应用凭据。`client_secret` 为空时保留已保存的 secret：

```json
{"account": "main", "client_id": "12345", "client_secret": "…",
 "redirect_uri": "http://127.0.0.1:8765/oauth/callback"}
```

返回 `{"ok": true, "oauth": {...}}`；回调地址必须是 `http(s)` URL。

### `POST /api/oauth/start`

```json
{"account": "main"}
→ {"ok": true, "url": "https://bgm.tv/oauth/authorize?client_id=…&response_type=code&…"}
```

未保存 App ID / Secret 时返回 `400`。

### `POST /api/oauth/exchange`

粘贴回调地址或裸 `code` 均可（服务端会提取 `code` 参数）：

```json
{"account": "main", "code": "http://127.0.0.1:8765/oauth/callback?code=…&state=…"}
→ {"ok": true, "oauth": {...}, "tokens": 已写入状态库}
```

失败（code 过期/已使用、凭据不匹配）返回 `400` 与 Bangumi 的具体错误。

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
  网页端会先在浏览器里裁剪成 JPEG（最长边 2560px）再上传，接口仍按原始二进制校验；
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
