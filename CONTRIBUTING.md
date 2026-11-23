# 贡献指南

欢迎 issue 和 PR。这个项目的目标是：**正确性 > 覆盖率**——宁可少同步一集，也不要把别人的观看记录写错。

## 开发环境

```bash
git clone https://github.com/fnos2bgm/fnos2bgm.git
cd fnos2bgm
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest                # 应全部通过（离线）
ruff check src tests
```

代码要求：

* 只用标准库（运行时零依赖是刻意设计，别为了小功能引入 requests/httpx）；
* Python ≥ 3.11（用到 `tomllib`、`slots=True` 数据类）；
* 类型注解齐全，日志用 `get_logger(...)` 而不是 `print`；
* 面向用户/运维的文案用英文（日志、错误），文档中英双份。

## 加一个新的数据源（Jellyfin / Emby / Plex）

1. 在 `src/fnos2bgm/sources/` 下新建模块，实现 `SourceAdapter`：
   `prepare()` 与 `snapshot() -> SourceSnapshot`；
2. `SeriesRef.key` 必须稳定且是**季粒度**；`episode_count` 填该季已识别集数；
3. `update_time` → epoch 秒、`runtime` → 秒；
4. 在 `sources/__init__.py` 的 `SOURCES` 注册；
5. 在 `tests/` 里补一条用假数据构造的 `snapshot()` 测试，以及一条 `sync` 集成测试；
6. README 的「实测环境」和 roadmap 同步更新。

## 改匹配算法

`matcher.py` 的评分改动**必须**配测试。建议同时跑一次真实库对比：

```bash
# 需要 NAS 上的一份快照
fnos2bgm --config config.toml dump --json > /tmp/before.json
# 改代码
fnos2bgm --config config.toml dump --json > /tmp/after.json
fnos2bgm review          # 复核队列变长通常意味着变保守了，变短要确认没有错配
```

判断标准：**错配（把 A 写到 B）权重远高于漏配**。如果一次调整让某部番从「自动」变成「复核」，
通常是可接受的；反过来需要证据。

## 提交 PR

* 一个 PR 一件事，附上动机和（如果和真实数据有关）脱敏后的样本；
* 新增行为要有测试；修 bug 请先写能复现的测试；
* 不要提交 `data/`、`config.toml`、`*.db`（`.gitignore` 已覆盖）；
* 截图/日志请去掉自己的 token、IP、NAS 路径中的敏感部分。

## 发布

`pyproject.toml` 里的版本号 + 打 tag `vX.Y.Z` 会触发多架构镜像与 sdist 发布；
CHANGELOG 按 Keep a Changelog 维护。
