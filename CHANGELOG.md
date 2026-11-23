# 变更记录

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与
[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] - 2026-11-23

首个可用版本，在 fnOS 1.2.1102 / 飞牛影视实测库（13 季 / 296 集）上验证。

### 新增

- 飞牛影视 `trim.media` 数据源：只读 `VACUUM INTO` 一致性快照、schema 列探测、
  可覆盖 SQL（`episodes_query` / `plays_query` / `users_query`）。
- 仅同步动漫：按媒体库路径前缀过滤，电视剧/电影/直播流自动排除；可跳过特别篇（Season 0）。
- 多用户：每个飞牛账号独立计算进度，可映射到各自的 Bangumi 账号，可单独覆盖观看阈值。
- "看完"判定：以进度/片长比例为主（默认 90%），兼顾 `watched` 标记与最短时长保护。
- Bangumi v0 集成：OAuth 授权码流程（本地回调或 `--manual`）、令牌自动刷新、
  按账号限速与指数退避重试、章节收藏与条目收藏状态读写、远端收藏核对。
- 标题匹配：标题归一化（含季标记剥离）、年份/集数/季号加权评分、歧义判定、
  候选列表落库供人工复核。
- 区间映射：支持一个本地 Season 拆分到多个 Bangumi 条目（合并季）。
- 幂等推送：`pushes` 表记录 `sent` / `skipped` / `failed` / `unmapped`，不重复写入。
- 只读状态页（`/`、`/api/status`、`/healthz`）与 JSON 日志。
- CLI：`run` / `oauth` / `status` / `review` / `map` / `search` / `dump`。
- Docker 镜像（零运行时依赖）、docker compose 示例、GitHub Actions CI 与多架构发布工作流。
- 81 个离线测试（模拟飞牛库 + 假 tracker），覆盖解析、匹配、幂等、拆季、鉴权失败等场景。

[Unreleased]: https://github.com/fnos2bgm/fnos2bgm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fnos2bgm/fnos2bgm/releases/tag/v0.1.0
