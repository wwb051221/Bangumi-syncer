# 安全策略

## 报告问题

请**不要**在公开 issue 里贴出：

* Bangumi `client_secret` / access token / refresh token；
* 完整的 `config.toml`、`/data/state/state.db`；
* 你的公网地址、飞牛账号、内网拓扑。

私下报告：发邮件到维护者邮箱（见 GitHub 个人资料），或用 GitHub 的
"Report a vulnerability" 私密通道。我们会在 7 天内回应。

## 本项目的安全模型

| 面 | 处理方式 |
| --- | --- |
| 源数据库 | **只读**挂载，程序只执行 `VACUUM INTO` 到自己的数据卷，从不写 `trimmedia.db` |
| 令牌存储 | `/data/state/state.db`（SQLite），不写配置文件、不进日志；日志里只出现账号名 |
| 配置文件的密钥 | 支持 `${ENV_VAR}` 展开，可让 `config.toml` 保持无密可读 |
| 网络出站 | 仅 `api.bgm.tv`（数据）与 `bgm.tv`（OAuth）；无遥测、无第三方统计 |
| 状态页 | 只读，无写接口；建议只在局域网暴露，`server.listen` 可改为 `127.0.0.1:8099` |
| 容器权限 | 默认 root **仅**因为 fnOS 数据库是 `root:700`；可改用 snapshot 模式后以非 root 运行 |
| 依赖 | 运行时零第三方依赖，攻击面即标准库 |

## 已知限制

* 容器能读飞牛数据库，因此**任何能执行容器命令的人**都等于能读该数据库；请限制 NAS 的 Docker 权限。
* OAuth 回调默认监听 `127.0.0.1:8765`；`--manual` 模式无需端口。
* 状态库未加密（依赖文件权限）。共享 NAS 上建议 `chmod 600`。
