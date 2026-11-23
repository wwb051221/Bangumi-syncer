# 架构

## 一句话

**读一份只读快照 → 算每个用户看完的集 → 匹配 Bangumi 条目 → 幂等推送章节收藏 → 维护收藏状态。**

## 模块图

```
cli.py                 命令分发 / 常驻循环 / 信号处理 / 退出码
 ├── config.py         TOML 解析、${ENV} 展开、校验
 ├── state.py          SQLite：令牌、映射、推送记录、候选、运行历史
 ├── sources/
 │    ├── base.py      SourceAdapter 抽象
 │    └── fnos.py      飞牛 trim.media：快照 + schema 探测 + 路径过滤
 ├── trackers/
 │    ├── base.py      Tracker 抽象 + Bangumi 常量
 │    └── bangumi.py   v0 API：OAuth、限速、重试、章节/收藏读写
 ├── matcher.py        标题归一化 + 评分 + 决策（自动/复核）
 ├── sync.py           SyncEngine：编排、幂等、失败隔离
 ├── server.py         只读状态页 + /api/status + /healthz
 └── oauth.py          授权码流程（本地回调或手动粘贴）
```

## 一次运行的流程

```
SyncEngine.run()
 ├─ store.start_run()                       记一条运行
 ├─ source.prepare()                         live: VACUUM INTO 快照 + quick_check + schema 探测
 ├─ source.snapshot()                        → SourceSnapshot {users, series, plays}
 ├─ 对每个 [[users]]:
 │   ├─ 找到对应飞牛账号（用户名或 guid）
 │   ├─ is_watched(play, ratio, min_secs)    进度比例 or watched=1
 │   └─ 按「季」分组（series.key = fnos:<tv_guid>:s<n>）
 │       ├─ 映射解析（先查 state，再自动匹配）
 │       ├─ 每集：本地第 N 集 → 条目第 N-ep_from+1 集
 │       ├─ 幂等：pushes 表已 sent → 跳过；verify_remote 时再核对远端
 │       └─ 收藏状态：有看完 → 在看；已播集全看完 → 看过
 └─ store.finish_run(...)                   统计与错误写入 runs
```

### 幂等与失败策略

* **幂等**靠 `pushes(account, series_key, episode)` 主键：`sent` / `unmapped` 直接跳过。
* `verify_remote = true` 时额外读一次 Bangumi 的章节收藏，把已存在的标记为 `skipped`。
* 错误分三层：
  * `AuthError`（令牌失效）→ 整轮中止，退出码 3，日志提示重跑 `oauth`；
  * `TrackerError`（单条目/单集失败）→ 记录 `failed`，继续下一部；
  * 其它异常 → 记录到 `runs.message`，循环不退出（容器不崩）。
* 修改映射（`map add/remove`）会删除对应区间的 `pushes`，下一轮重新推送。

### 并发

* 主线程跑同步循环，一个 daemon 线程跑状态页；`StateStore` 内部用 `RLock` 串行化，
  SQLite 开了 `wal` + `busy_timeout=30s`。
* HTTP 客户端按账号限速（`min_interval_seconds`），同步循环与状态页共享同一批客户端实例。

## Web 层

* `server.py` 在同步循环同一进程里起一个 `ThreadingHTTPServer`（daemon 线程），
  默认 `0.0.0.0:8099`。
* 前端是 `src/bangumi_syncer/web/` 下的原生 JS 单页应用（`index.html` / `app.js` / `style.css`），
  通过 `importlib.resources` 随包分发，**没有构建步骤**。
* `GET /api/status` 一次返回界面所需的全部数据（含打码配置与最近日志）；
  `GET /api/logs` 供日志页轮询；`GET /api/search` 代理 Bangumi 公开搜索。
* 写操作（`POST /api/run`、`POST|DELETE /api/map`、`DELETE /api/candidates`）要求
  `X-Requested-With: bangumi-syncer`，服务端不返回 CORS 头，跨站表单无法调用。
* 「立即同步」通过 `StatusState.run_requested`（`threading.Event`）通知循环；
  循环在 `interval` 等待中每 0.5 秒检查一次，因此最迟半秒内开始新的一轮，不需要第二个执行线程。
* 日志页数据来自挂在 root logger 上的环形缓冲 handler（500 行），随 `StatusServer.stop()` 移除。
* 接口清单见 [api.md](api.md)。

## 运行时设置（Web 可编辑）

`config.toml` 仍是密钥、挂载与监听地址的来源；其余运维参数由 `settings.py` 的
`SettingsManager` 管理：

* 启动时读取状态库 `meta.runtime_settings`（JSON），校验后**直接改写内存里的 `Config` 对象**；
* 同步引擎每轮都从 `Config` 读取参数，因此保存后下一轮即生效，不需要重启容器；
* 修改通过 `PUT /api/settings` 提交，校验失败返回 400 与具体字段；
* 「飞牛用户」列表由 `GET /api/source-users` 现读媒体库得到（缓存 60 秒），
  设置页把「媒体库真实账号」与「当前配置用户」取并集，媒体库中已消失的账号会被标注；
* `POST /api/settings/reset` 删除覆盖层并恢复启动时的基线值（来自 `config.toml`）。

前端个性化（主题、主题色、字号、密度）只存在浏览器 `localStorage`，不写入服务端；
`index.html` 里有一段内联引导脚本，在首屏渲染前应用主题，避免闪白。

## 外观与背景（服务器端保存）

`ui.py` 的 `UiManager` 负责主题、主题色、字号、密度、背景与莫兰迪配色：

* 配置保存在状态库 `meta.ui_prefs`（JSON，`rev` 每次修改递增）；
* 背景图片保存在 `<state 目录>/assets/background.bin`，由 `GET /static/background` 提供，
  带 `ETag` 与 1 小时缓存；
* 背景来源：上传（校验 PNG/JPEG/WebP/GIF 魔数、≤ 8 MB）、URL（服务端下载后落盘，
  避免跨域与图床失效）、内置渐变预设（纯 CSS，无文件）；
* 莫兰迪配色在**浏览器**里用 canvas 取样（服务器不需要图像库），按色相去重、把饱和度压到
  10%–30%、亮度收进 36%–68%，结果 `PUT /api/ui/palette` 存回服务器；
* 前端首屏用 localStorage 缓存快速应用外观，随后以 `/api/status` 里的 `ui` 为准，
  `rev` 变化时自动重新应用，多设备最终一致。

## 扩展点

### 新的媒体服务器

实现 `sources/base.py::SourceAdapter`：

```python
class JellyfinSource(SourceAdapter):
    name = "jellyfin"

    def prepare(self) -> None:
        """登录、或确认数据可读。"""

    def snapshot(self) -> SourceSnapshot:
        """返回 users / series / plays，全部使用 models.py 里的数据结构。"""
```

然后在 `sources/__init__.py` 的 `SOURCES` 里注册。要求：

* `SeriesRef.key` 必须**稳定**（跨运行不变），并且**季粒度**；
* `SeriesRef.episode_count` 填该季已识别的集数（影响匹配打分）；
* `PlayState.episode.runtime_seconds` 用秒，未知填 `None`（此时只认 `finished_flag`）；
* 路径过滤交给 `fnos.path_allowed`（可复用）或自己实现。

`details`：Jellyfin 可以直接给出 `Played` / `PlaybackPositionTicks` / `RunTimeTicks`，
对应 `finished_flag` / `position_seconds` / `runtime_seconds`；`UserData` 天然多用户。

### 新的目标站点

实现 `trackers/base.py::Tracker` 的七个方法（`ensure_ready` / `search` / `episodes` /
`subject_name` / `collection` / `set_collection` / `episode_marks` / `mark_episode`），
在 `trackers/__init__.py` 注册。`sync.py` 只依赖这些方法，不关心是 Bangumi 还是 MAL。

## 状态库

`/data/state/state.db`（SQLite，WAL）主要表：

| 表 | 用途 |
| --- | --- |
| `accounts` | 每个 Bangumi 账号的 access/refresh token、过期时间、远端用户名 |
| `mappings` | `(account, series_key, ep_from..ep_to) → subject_id`，区间最窄者优先 |
| `pushes` | `(account, series_key, episode)` 的推送状态：`sent` / `skipped` / `failed` / `unmapped` |
| `candidates` | 未匹配条目的候选列表与原因（复核队列） |
| `runs` | 每次运行的统计与错误 |

## 测试策略

* `tests/conftest.py` 用真实 schema 造一个**模拟飞牛库**（含直播、电影、电视剧、特别篇、
  合并季、旧播放记录等噪声）。
* `FakeTracker` 在内存里实现 Tracker，断言推送的 `(subject, episode_id)` 序列。
* `test_bangumi.py` 用脚本化的 HTTP 替身验证真实 tracker 的请求构造：搜索类型过滤、
  章节分页、收藏 POST/PATCH、章节标记、404 语义。
* `test_cli.py` 直接调 `cli.main([...])` 跑 `dump` / `status` / `map` / `review` / `run --once`，
  验证退出码与输出（不联网）。
* 覆盖：快照解析、过滤、比例判定、区间映射、幂等、远端核对、收藏完成、鉴权失败、限速重试。
* 全部离线（160 个用例，约 17 秒）。
