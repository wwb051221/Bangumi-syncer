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
* 全部离线（103 个用例，约 7 秒）。
