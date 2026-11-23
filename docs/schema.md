# 飞牛影视数据库结构（实测）

> 实测版本：fnOS **1.2.1102**，`trim.media` 系统应用，2026-11。
> 这份文档是 `sources/fnos.py` 的默认 SQL 的依据；飞牛升级后如果对不上，按本文末的
> 「覆盖查询」调整配置即可，无需改代码。

## 位置与权限

```
/usr/local/apps/@appdata/trim.media/database/trimmedia.db      media 库（本工具用这个）
/usr/local/apps/@appdata/trim.media/database/trimactivity.db   登录/访问日志
/usr/local/apps/@appdata/trim.media/database/trimmedia_ext.db  刮削缓存
```

* 目录与文件都是 `root:root 0700/0600`，SMB、普通用户、非 root 容器都读不到。
* 实测 `journal_mode = delete`（**不是 WAL**，早期环境说明里写的 WAL 有误）。
* 应用常驻写库，但 `VACUUM INTO` 只需读锁，不会阻塞播放。

## 层级

```
item.type = 'TV'         剧集/番剧（有 number_of_seasons）
  └─ item.type = 'Season'    season_number
       └─ item.type = 'Episode'   season_number, episode_number, runtime(分钟)
item.type = 'Movie'      电影/剧场版（无父子）
item.type = 'LiveChannel' 直播频道（也会写播放记录）
item.type = 'Directory' / 'MediaDB' / 'Video'  内部结构
```

实测某库规模：`item` 1658 行（LiveChannel 968、Episode 438、Directory 180、Movie 25、Season 18、TV 13）。

## 关键表

### `item`

| 列 | 说明 |
| --- | --- |
| `guid` | 主键 |
| `type` | TV / Season / Episode / Movie / LiveChannel / … |
| `title` | Episode 常是「第 4 集」，剧名要看父级 |
| `original_title` | 原名（多为日文/英文） |
| `alternative_titles` | JSON：`{"titles":[{"title":"…"}]}`，可作匹配别名 |
| `tmdb_id` | TMDB 编号；**没有 Bangumi ID**，`external_ids` 实测全为空 |
| `runtime` | **分钟**，部分老片为 0 |
| `parent_guid` | 父级 guid |
| `season_number` / `episode_number` | 季/集号 |
| `first_air_date` | `YYYY-MM-DD`，在 TV 上表示**整部首播年**（多季时不是本季年份） |
| `number_of_seasons` / `number_of_episodes` | 剧集级信息 |

### `item_user_play`

| 列 | 说明 |
| --- | --- |
| `item_guid` | 指向 Episode / Movie / LiveChannel |
| `user_guid` | 用户 |
| `ts` | **播放位置，秒** |
| `watched` | 0/1 看完标记（**不可靠**：片尾退出常为 0） |
| `update_time` | **毫秒**时间戳 |
| `type` | `Episode` / `Movie` / `LiveChannel`，可直接用来过滤 |
| `resolution` / `bitrate` | 播放方式 |

同一 `(user_guid, item_guid)` 可能有多条历史记录，默认 SQL 取 `update_time` 最大的那条。

### `item_media`

| 列 | 说明 |
| --- | --- |
| `item_guid` | 对应条目 |
| `path` | 宿主机绝对路径，如 `/vol1/1000/影片/动漫/药屋少女的呢喃 (2023)/药屋少女的呢喃 (2023) S01E01.mp4` |
| `sort_num` | 同一集多文件时的顺序 |

直播频道的 `path` 是 URL（`https://…/live.m3u8`），因此按路径前缀过滤动漫库时天然被排除。

### `"user"`

| 列 | 说明 |
| --- | --- |
| `guid` | 用户 guid（播放记录里的 `user_guid`） |
| `username` | 登录名 |
| `passwd` | argon2id 哈希（本工具不读） |
| `status` | 1 = 正常；`default-user-template` 是模板账号 |

实测账号数与播放记录分布：

| 用户 | Episode 播放记录 | 其中 watched=1 |
| --- | --- | --- |
| 管理员 | 3（动漫库） | 0（全靠 `ts` 推比例） |
| 用户 A | 96 | 19 |
| 用户 B | 13 | 8 |

因此**必须按用户分开统计**，且不能只信 `watched`。

## 本工具使用的默认查询

三条独立查询，Python 里按 `guid` 关联（比一条巨型 JOIN 更好替换、更好测试）：

1. `episodes_query`：所有季的所有集（TV → Season → Episode + 第一条 `item_media.path`）
2. `plays_query`：每个用户每集最新的一条播放记录（含 `ts` / `watched` / `update_time`）
3. `users_query`：所有正常账号（用于区分「没看过动漫」与「用户不存在」）

完整 SQL 见 [`src/fnos2bgm/sources/fnos.py`](../src/fnos2bgm/sources/fnos.py) 里的
`DEFAULT_EPISODES_QUERY` / `DEFAULT_PLAYS_QUERY` / `DEFAULT_USERS_QUERY`。

## 覆盖查询（升级适配）

当飞牛改了表结构，`prepare()` 会报错并列出缺失的列。此时在配置里提供替代 SQL：

```toml
[source.fnos]
episodes_query = """select ... """
plays_query = """select ... """
users_query = """select ... """
```

**必须是只读 SELECT，且列名固定**（别名要对齐）：

| 查询 | 必需列 |
| --- | --- |
| `episodes_query` | `tv_guid`, `tv_title`, `tv_original_title`, `tv_first_air_date`, `tv_tmdb_id`, `tv_alt_titles`, `season_number`, `ep_guid`, `episode_number`, `ep_title`, `ep_runtime`, `media_path` |
| `plays_query` | `user_guid`, `username`, `ep_guid`, `ts`, `watched`, `update_time` |
| `users_query` | `user_guid`, `username` |

注意事项：

* `ep_runtime` 用**分钟**（适配器内部 ×60）；
* `update_time` 用**毫秒**（适配器内部 ÷1000）；
* `ep_guid` 是两张表的关联键，必须一致；
* `media_path` 为 NULL 时，如果配置了 `include_paths`，该集会被过滤掉。

## 一致性快照

不要直接读跨网络的库文件。两种做法：

**A. 容器内自己做（默认，推荐）** —— `source.mode = "live"`：

```python
sqlite3.connect("file:/source/trimmedia.db?mode=ro", uri=True).execute(
    "VACUUM INTO '/data/snapshot/origin.db'"
)
```

只读连接 + `VACUUM INTO` 已验证可用（Python 3.12 / SQLite 3.49，库为 journal=delete）。
快照后还会跑一次 `pragma quick_check`。

**B. 宿主 cron 生成，容器只读快照** —— `source.mode = "snapshot"`：

```bash
# root crontab，每 5 分钟
/vol1/@appcenter/nodejs_v22/bin/node -e '
const {DatabaseSync}=require("node:sqlite");
const db=new DatabaseSync("/usr/local/apps/@appdata/trim.media/database/trimmedia.db",{readOnly:true});
db.exec("VACUUM INTO \x27/vol1/1000/下载/trim-sync/trimmedia.db\x27");
db.close();'
```

飞牛自带 Node v22（`/vol1/@appcenter/nodejs_v22/bin/node`），没有 `sqlite3` 命令行。
