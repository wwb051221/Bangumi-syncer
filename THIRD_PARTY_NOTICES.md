# 第三方组件与致谢

本仓库**没有复制或内嵌其他项目的源代码**：后端只使用 Python 标准库，前端为手写原生
JavaScript/CSS（无 npm 依赖、无 CDN 资源）。因此不存在需要逐文件标注作者/来源的 vendored 代码。

下面列出构建、运行和文档中实际使用的外部组件与数据来源，便于合规审计和致谢。

## 运行时与容器镜像

| 组件 | 用途 | 许可 / 说明 |
| --- | --- | --- |
| [Python](https://www.python.org/) 3.11+ | 运行时 | PSF License 2.0 |
| [`python:3.12-slim`](https://hub.docker.com/_/python) | 官方基础镜像 | 基于 Debian slim；Python 与 Debian 各软件包的许可证文本随镜像分发，位于 `/usr/local/lib/python3.12/LICENSE.txt` 与 `/usr/share/doc/*/copyright` |

本项目不会修改或重新打包基础镜像中的第三方程序，只在其上添加自己的 Python 源码。

## 构建、测试与 CI（不进入运行时镜像）

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| [pytest](https://github.com/pytest-dev/pytest) | 单元测试（dev extra） | MIT |
| [ruff](https://github.com/astral-sh/ruff) | lint / 格式化（dev extra） | MIT |
| [build](https://github.com/pypa/build) | Release 构建 sdist/whl | MIT |
| [actions/checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[upload-artifact](https://github.com/actions/upload-artifact) | GitHub Actions | MIT |
| [docker/setup-qemu-action](https://github.com/docker/setup-qemu-action)、[setup-buildx-action](https://github.com/docker/setup-buildx-action)、[login-action](https://github.com/docker/login-action)、[metadata-action](https://github.com/docker/metadata-action)、[build-push-action](https://github.com/docker/build-push-action) | 多架构镜像构建与发布 | Apache-2.0 |
| [softprops/action-gh-release](https://github.com/softprops/action-gh-release) | 创建 GitHub Release | MIT |

## 外部服务与数据来源

| 服务 | 用途 | 说明 |
| --- | --- | --- |
| [Bangumi 番组计划](https://bgm.tv/) / [api.bgm.tv](https://api.bgm.tv/) | 条目搜索、章节与收藏同步 | 通过公开 API 与 OAuth 访问；本项目与 Bangumi 官方无隶属关系，使用需遵守其服务条款 |
| 飞牛影视（fnOS `trim.media`） | 只读读取本地媒体库 SQLite | 闭源商业软件；本项目与其无隶属关系，仅以只读方式读取数据库，不修改其文件 |
| [shields.io](https://shields.io/) | README 徽章图片 | 仅引用在线图片，不随仓库分发 |

## 文档与素材

- `docs/screenshots/` 下的截图由本项目自行生成。
- favicon 为内联手写 SVG；界面字体使用系统字体栈（`system-ui` 等），未打包第三方字体或图标库。
- 飞牛数据库结构说明来自对公开数据库文件的只读观察与整理，未复制其源代码或文档。

## 项目自身许可

本项目以 [MIT License](LICENSE) 发布，版权归 Bangumi-syncer contributors 所有。
如果你在二次分发时修改了上述第三方组件，请同时遵守对应组件的许可证。
