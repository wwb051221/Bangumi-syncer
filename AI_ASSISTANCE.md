# AI 辅助开发声明

本项目在开发过程中使用了 AI 编码智能体，为提高透明度在此说明所用工具、模型与范围。

## 使用的智能体与模型

| 项 | 值 |
| --- | --- |
| 智能体 | **DeepSeek Harness（DSH）编码智能体** |
| 智能体版本 | **0.2.2-rc.5**（桌面端 `@deepseek-ai/dsh-desktop` 与内置运行时 `@deepseek-ai/dsh-desktop-runtime` 相同） |
| 模型 | **`deepseek-flash`**（会话路由标识；运行时未提供单独的语义版本号） |
| 使用时间 | 2026-11-24 至 2026-11-26（Asia/Shanghai） |
| 交互方式 | DSH Web GUI / 桌面端，配合文件编辑、终端、测试、浏览器与 SSH 工具 |
| 人工角色 | 项目维护者 `wwb051221` 提出需求、确认方案并审核发布内容 |

## 智能体参与的范围

- 后端 Python 实现、SQLite 只读快照、匹配与同步逻辑
- 前端原生 JS/CSS、响应式与深色适配、可视化配置页
- 鉴权（PBKDF2 + 签名 Cookie）、OAuth 回调与凭据管理
- 测试、Docker/Compose、GitHub Actions 与发布流程
- 文档、截图、验证记录与部署脚本

## 质量与原创性

- 所有代码变更均经过自动化测试（pytest、ruff、CI）与人工验收
- 未复制或内嵌其他仓库的源代码；第三方组件与数据来源见
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- AI 生成内容可能包含错误，使用前请以仓库中的测试、文档与发布说明为准
- 本声明不改变项目的 [MIT License](LICENSE)

## 如何引用

如果在本项目基础上做研究或二次开发，请引用仓库（见 [CITATION.cff](CITATION.cff)）：

```bibtex
@software{bangumi_syncer_2026,
  title        = {Bangumi-syncer},
  author       = {{Bangumi-syncer contributors}},
  year         = {2026},
  version      = {0.1.0},
  url          = {https://github.com/wwb051221/Bangumi-syncer},
  license      = {MIT},
  note         = {Developed with DeepSeek Harness (DSH) 0.2.2-rc.5, model deepseek-flash}
}
```

引用时建议同时注明访问日期与实际使用的版本（`v0.1.0` 或对应 Git commit）。
