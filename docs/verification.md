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

## 4. Web 界面（容器内验证）

| 项 | 结果 |
| --- | --- |
| SPA 首页 | ✅ `GET /` 200，含应用壳（`id="app"`）、引用 `/static/app.js` 与 `/static/style.css` |
| 静态资源 | ✅ `app.js` 27.6 KB、`style.css` 8.0 KB（`Cache-Control: max-age=300`） |
| `/api/status` | ✅ 返回 `version/dry_run/run_requested/source/stats/users/accounts/runs/mappings/review/logs/config` |
| CSRF | ✅ 无 `X-Requested-With` 的 `POST /api/run` → **403**；带头 → **202** |
| 「立即同步」 | ✅ 0.5 秒内被循环拾取，日志出现 `manual sync request picked up` |
| 映射写入 | ✅ `POST /api/map` 创建成功，`/api/status` 立即可见；`DELETE /api/map` 删除成功 |
| 条目搜索 | ✅ 容器内真实调用 Bangumi：`药屋少女的呢喃` → 420628 / 486347 |
| 未知路由 | ✅ 404 JSON |
| 打包 | ✅ `pip install .` 后 `importlib.resources` 能找到 3 个前端资源 |

## 5. 界面：设置页、深浅色、响应式

用 `puppeteer-core` 驱动本机 Chrome（无头）访问运行中的容器，逐档截图并读取真实布局指标
（`documentElement.scrollWidth` vs `clientWidth`，以及页面自报的 `data-layout`）。

| 视口 | 页面 | 主题 | 结果 |
| --- | --- | --- | --- |
| 320×640 | 概览 / 设置 | 浅色 | ✅ 无横向溢出 |
| 390×844 | 概览 / 映射 / 设置 | 浅色 / 深色 | ✅ |
| 768×1024 | 概览 / 设置 | 深色 / 浅色 | ✅ |
| 1024×768 | 映射 / 设置 | 浅色 / 深色 | ✅ |
| 1366×768 | 设置 | 深色 | ✅ |
| 1920×1080 | 设置 | 浅色 | ✅ |
| 2560×1080 | 概览 | 深色 | ✅ |
| 1024×768（模拟系统偏好） | 概览 | 跟随系统 → 浅 / 深 | ✅ 解析正确 |

共 **17 个用例，0 失败**，无控制台错误；截图存于 [`docs/screenshots/`](screenshots/)。

设置页功能验证（真实容器 + 真实媒体库）：

* `GET /api/settings` 返回配置值与媒体库真实账号（4 个用户及播放/看完计数）；
* `PUT /api/settings` 把间隔改为 180 秒并禁用 `2708` → **重启容器后依然生效**，
  循环日志显示 `interval 180s`、`1 configured user`，说明持久化与热生效都正确；
* `POST /api/settings/reset` 恢复 `config.toml` 的值；
* 深浅色三档、6 主题色、字号、密度通过 `?theme=` 与 localStorage 验证，首屏无闪白。

### 外观与背景（服务端保存）

在真实容器里用无头 Chrome 走完整交互：

| 项 | 结果 |
| --- | --- |
| 上传背景 | 87 KB 渐变 PNG → `PUT /api/ui/background`，落盘 `data/state/assets/background.bin`，`GET /static/background` 返回 `image/png` |
| 背景渲染 | `data-bg="image"`，层 `display:block`、`opacity:0.5`、`blur(6px)`、遮罩 0.5 |
| 从背景取色 | canvas 取样得到 6 个莫兰迪色（`#b1a5b6` `#a5a9b6` `#a5aeb6` `#b6a5a5` `#b6ada5` `#b6a5af`），DOM 与服务器一致 |
| 应用配色 | 点击色块 → `accent` 持久化为 `#b1a5b6`（写回服务器） |
| 多设备同步 | 新开无痕上下文（无 localStorage）读到服务器主题 `light`；`PUT theme=dark` 后刷新即为 `dark` |
| 响应式回归 | 开启背景后 17 个视口用例仍全部通过，0 横向溢出 |

### 背景裁剪与预览

用无头 Chrome 走完「选择文件 → 裁剪 → 上传」，实测结果：

| 项 | 结果 |
| --- | --- |
| 打开裁剪 | 1600×900 原图，舞台比例 1.776（16:9），预览画布 640×360 且非空 |
| 拖动 | 预览内容变化（哈希对比） |
| 缩放 | 滑杆 180% → 标签与画布同步 |
| 比例 | 切到 1:1 后预览为 500×500 |
| 输出 | 确认后上传 JPEG（7.2 KB），背景版本 3→4，`/static/background` 返回 `image/jpeg` 且魔数 `FF D8 FF` |
| 重新裁剪 | 打开服务器背景（889×500）再次裁剪 |
| 移动端 | 390×844 下弹窗宽 360、页面 `layout=ok`，预览完整 |
| 控制台 | 无报错（过程中修复了关闭弹窗后预览定时器访问已释放图片、舞台宽度收缩两个问题） |

### 圆角一致性

| 元素 | 计算值（舒适密度） | 紧凑密度 |
| --- | --- | --- |
| 卡片 / 表格容器 | 13.6px（`--radius-lg` .85rem） | 11.2px（.7rem） |
| 按钮 / 输入框 | 9.28px（`--radius-md` .58rem） | 7.68px（.48rem） |
| 徽章 / 分段 | 999px / 9.28px | 同左 / 7.68px |
| `table` 本体 | 0（由 `.table-wrap` 裁剪，`overflow:auto` + 圆角） | — |

表格此前因 `border-collapse: collapse` 无法显示圆角，现改为容器承担边框与裁剪，
`table` 只保留内容；开启自定义背景时容器使用半透明毛玻璃，表格背景透明，圆角处不再出现直角溢出。

## 6. 移动端下拉与深色适配

Android Firefox 的原生 `<select>` 弹层由系统绘制，不跟随页面深色配色：白底 + 继承的浅色文字，
选项几乎不可见（实机截图确认）。现改为自绘 `.select`（按钮 + 列表 + 隐藏 input，id/value 接口不变）：

| 检查 | 结果 |
| --- | --- |
| 原生 `<select>` 数量 | 0（映射账号、用户账号、背景预设均已替换） |
| 弹层定位 | 挂到 `<body>`，`position:fixed`，不被 `.table-wrap` 的 `overflow`/`backdrop-filter` 裁剪 |
| 深色配色 | 背景 `rgb(23,26,33)`，文字 `rgb(230,232,238)`，对比度检查通过 |
| 视口内 | 390×844 下弹层完全位于视口内 |
| 选择行为 | 隐藏 input 与按钮文案同步更新；表格内下拉同样可用 |
| 关闭 | 点外部、Esc、滚动（打开后 350ms 内的轻微滚动不触发） |
| 背景预设 | 选择「暮色」后 `value=dusk`、文案「暮色」 |
| 移动端表单 | 字段与按钮整行排布，无窄输入框 |

## 7. 媒体库目录可视化选择

目录树来自飞牛数据库中的真实剧集路径（未过滤全量快照），在容器内实测：

| 检查 | 结果 |
| --- | --- |
| 目录树 | 1 个根节点 `/vol1/1000/影片`（17 季 437 集），子节点「动漫」13 季 296 集、「电视剧」4 季 141 集 |
| 当前选择 | include 路径 `/vol1/1000/影片/动漫` 自动勾选，父节点显示半选（indeterminate） |
| 勾选子目录 | 勾选「电视剧」后 chip 由 1 条变 2 条，子项显示「已由上级目录包含」 |
| 手动添加/移除 | `/vol9/手动/测试` 添加后出现在 chip，点 × 可移除 |
| 保存 | 点击保存后 `GET /api/settings` 的 `include_paths` 与界面一致（测试已恢复原值） |
| 移动端 | 390px 下目录选择改为单列，`layout=ok`，无横向溢出 |
| 控制台 | 无报错；后端新增 4 个目录树单测，设置/接口测试补充断言 |

## 8. Bangumi 授权可视化

在真实容器上验证设置页的授权流程（当前实例尚未填入真实应用凭据，交换步骤由单测覆盖）：

| 检查 | 结果 |
| --- | --- |
| 初始状态 | 账号 `main`、App ID 为空、Secret 占位「粘贴 App Secret」、回调地址默认值、「未授权」 |
| 无凭据生成链接 | 返回 `400`，页面 toast：「请先填写并保存 App ID / App Secret」 |
| 手动指引 | 折叠在「手动配置指引」中，默认收起，内容仍完整 |
| 操作栏圆角 | `.settings-actions` 计算值 `border-radius: 13.6px`，半透明浮动面板，不再整条方角横条 |
| 移动端 | 390px 下单列、`layout=ok`、无横向溢出 |
| 接口 | `GET/PUT /api/oauth`、`start`、`exchange` 均有 pytest 覆盖；secret 不出现在任何响应中 |
| 交换授权 | 单测用假 `exchange_code` 验证：token 入库、remote_user 写入、空 code 返回 400 |

## 9. 快照健壮性（实机发现）

飞牛在 01:45 / 01:47 重写 `trimmedia.db` 时，只读 `VACUUM INTO` 会得到**合法但无表**的空库，
`quick_check` 仍返回 ok，旧版本会因此整轮失败。现在快照后会校验必需表：不完整时保留上一份
可用快照并告警（日志 `keeping the previous snapshot`），同步照常完成；只有完全没有旧快照时才
报错并在下一轮重试。已补 3 个回归测试。

## 10. 未验证 / 已知限制

| 项 | 说明 |
| --- | --- |
| **真正写入 Bangumi** | 需要用户在 <https://bgm.tv/dev/apps> 自建应用并授权，仓库不含任何令牌；代码路径已由假 tracker + HTTP 替身测试覆盖 |
| OAuth 回调 | 本地回调与 `--manual` 两种模式均有实现，但未用真实 client_id 走通（同上） |
| arm64 镜像 | 本机与 NAS 均为 amd64；CI 的 release 工作流会构建 `linux/amd64,linux/arm64`，未在真机验证 |
| 飞牛版本差异 | 仅 1.2.1102 实测；schema 探测 + 可覆盖 SQL 用于兜底 |
| Windows 容器 | 不支持（镜像基于 Linux） |

## 11. 复现步骤

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
