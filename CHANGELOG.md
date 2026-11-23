# 变更记录

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与
[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **背景上传支持裁剪与实时预览**：选择图片后弹出裁剪窗口，可拖动定位、滚轮/滑杆缩放、
  选择 16:9 / 21:9 / 4:3 / 1:1 比例；右侧实时显示裁剪结果与「页面效果」预览（含遮罩）。
  输出 JPEG（最长边 2560px，质量 0.9）后上传；已保存的背景可「重新裁剪」。
- **外观配置改为服务器端保存**（状态库 + `assets/background.bin`），主题/主题色/字号/密度/背景
  在所有设备间共享；浏览器仅保留首屏缓存以避免闪白。
- **自定义背景**：上传图片（PNG/JPEG/WebP/GIF，≤8MB）、图片 URL（服务端下载后保存）、
  4 个内置渐变预设（晨雾/暮色/深海/纸感），可调不透明度、模糊与遮罩。
- **从背景提取莫兰迪配色**：浏览器 canvas 取样，压饱和度、收亮度、按色相去重，
  取 6 色存入服务器，可一键设为主题色。
- 新增接口：`GET/PUT /api/ui`、`PUT/DELETE /api/ui/background`、`PUT /api/ui/palette`、
  `GET /static/background`。
- **可视化设置页**：在线修改同步开关/间隔/阈值、匹配参数、媒体库目录、飞牛用户；保存在状态库的
  运行时覆盖层里，**改完下一轮同步即生效，无需重启容器**；可一键恢复 `config.toml` 默认值。
- **飞牛影视用户选择**：设置页直接读取媒体库中的真实账号列表（播放记录数、已看完数），
  勾选参与同步的用户、指定 Bangumi 账号、单独设置观看阈值；媒体库里已消失的账号会被标注。
- **深浅色与界面个性化**：跟随系统 / 浅色 / 深色三档，主题色（6 个预设 + 自定义）、字号 85%–125%、
  舒适/紧凑密度；主题在首屏渲染前应用（无闪白），配置由服务器保存（见上方「外观配置」）。
- **Bangumi API 获取指引**：设置页内置建应用步骤、回调地址、`.env` 模板与 `oauth` 命令，
  每项都可一键复制。
- 新增接口：`GET/PUT /api/settings`、`POST /api/settings/reset`、`GET /api/source-users`。

### 修复

- **飞牛重写数据库时可能出现「空快照」**：只读 `VACUUM INTO` 会得到一个合法但无表的 SQLite 文件，
  `quick_check` 仍然通过，旧版本因此整轮同步失败。现在快照后校验必需表，不完整时保留上一份
  可用快照并记录告警，只有完全没有旧快照时才报错重试（实机已复现并验证修复）。
- `match.accept_types` 之前未生效（搜索硬编码为动画 type=2），现在会传给 Bangumi 搜索接口。
- `map add/list/remove` 的 `--account` / `--user` 必须写在动作之前才生效（与 README 用法不符），
  现在 `bangumi-syncer map add --account main ...` 可用。
- 鉴权失败时 `run --once` 现在返回退出码 **3**（文档一直这么写，之前返回 1）。

### 测试

- 新增外观相关用例（预置/夹取/背景魔数/落盘/URL 校验/配色接口），测试总数 155。
- 新增 17 个用例：CLI 端到端（`dump`/`status`/`map`/`review`/`run --once` 与退出码）、
  Bangumi tracker 请求构造（类型过滤、分页、收藏 POST/PATCH、章节标记、404）、
  `accept_types` 透传。共 103 个离线用例。

### 变更

- 项目更名为 **Bangumi-syncer**：仓库/发行名 `bangumi-syncer`、Python 包 `bangumi_syncer`、
  命令行 `bangumi-syncer`、镜像 `ghcr.io/wwb051221/bangumi-syncer`、容器名 `bangumi-syncer`。
  配置环境变量由 `FNOS2BGM_CONFIG` 改为 `BANGUMI_SYNCER_CONFIG`；旧状态库结构与行为不变。
- 构建支持 `BASE_IMAGE` build arg / `.env`，便于国内镜像源；详见 README。

### 验证

- 在 fnOS 1.2.1102 实机用 `docker compose build/run` 验证：live 模式快照真实库（13 季 / 296 集）、
  状态页健康检查、容器内直连 Bangumi 匹配（药屋→420628、魔卡→1940）。
- Web 界面在容器内验证：SPA/静态资源、CSRF 拦截、立即同步被拾取、映射增删、条目搜索。
  过程与结论见 [docs/verification.md](docs/verification.md)。

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

[Unreleased]: https://github.com/wwb051221/Bangumi-syncer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wwb051221/Bangumi-syncer/releases/tag/v0.1.0
