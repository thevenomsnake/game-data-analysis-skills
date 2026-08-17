# GitHub 仓库 README 最佳实践研究

> 文档类型：研究笔记
>
> 适用范围：仓库首页 README 的用途、内容组织和可读性
>
> 来源边界：仅 GitHub 官方文档与 GitHub 官方维护来源
>
> 最近验证：2026-08-04

## 结论摘要

仓库首页 README 应是新成员的**首次认识、首次跑通和后续导航页**，不是完整手册。GitHub 明确指出，README 往往是访客首先看到的内容，应回答项目做什么、为什么有用、如何开始、去哪里求助，以及谁在维护和贡献；同时，README 只应保留开始使用和参与项目所必需的信息，较长文档应另行承载。对本仓库而言，现有 `docs/` 可以承担 GitHub 文档所说的长篇文档角色。([About the repository README file](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes))

## 首页 README 应优先回答什么

推荐按新成员的阅读顺序组织，而不是按仓库目录或维护者心智组织：

| 优先级 | 新成员的问题 | README 应给出的最短答案 | 官方依据 |
|---|---|---|---|
| 1 | 这是什么，为什么需要它？ | 项目名称、一句话用途、核心价值和适用范围。 | [README 通常包含“做什么、为什么有用”](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#about-readmes) |
| 2 | 我是否具备开始条件？ | 目标读者、必要权限、工具或环境前置条件；非必要背景下沉。 | [Quickstart 应明确受众、前置条件和先验知识](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type#how-to-write-a-quickstart) |
| 3 | 怎样完成第一次成功使用？ | 一条推荐安装路径和一个可在短时间内完成的最小任务，并写明预期结果。 | [Quickstart 只保留完成聚焦任务的必要步骤](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type) |
| 4 | 卡住后去哪里？ | 帮助渠道，以及安装变体、故障排查和完整手册的直接入口。 | [README 应说明从哪里获得帮助](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#about-readmes) |
| 5 | 怎样参与，谁负责？ | 贡献指南入口、维护者或负责团队、必要的协作边界。 | [README 应说明维护者和贡献者](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#about-readmes)；[贡献指南的 GitHub 原生入口](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors#about-contributing-guidelines) |

版本号、状态或兼容性只有在会改变“能否开始”这一判断时才应进入首页；完整版本历史和兼容矩阵属于长篇文档。这是依据“只保留开始使用和贡献所必需信息”得出的内容取舍。([About READMEs：Wikis](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#wikis))

## README 与 docs 的边界

GitHub 的直接建议是：README 只包含开发者开始使用和参与项目所必需的信息，较长文档更适合 wiki；Quickstart 也应链接到其他资料而不是复制它们，以免打断主流程。将这一职责映射到本仓库，建议如下。([About READMEs：Wikis](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#wikis)) ([Quickstart content type](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type#how-to-write-a-quickstart))

| 留在首页 README | 下沉到 `docs/` 或专项文件 |
|---|---|
| 一句话定位、适用对象和核心用途 | 架构、领域概念、设计理由和模块关系 |
| 会阻断首次使用的前置条件 | 完整环境矩阵、平台差异、权限说明和配置参考 |
| 唯一推荐的安装路径 | 其他安装方式、升级、迁移、卸载和回滚 |
| 一个最小快速开始及其成功信号 | 长流程、角色专属工作流和扩展示例 |
| 帮助、完整文档和贡献入口 | 完整故障排查、运维手册和常见问题 |
| 维护者或负责团队 | issue、分支、提交、测试和 PR 细则，优先放入 `CONTRIBUTING.md` |

仓库内链接使用相对路径。GitHub 会按当前分支转换相对链接，而且相对链接对克隆后的仓库更可靠。([Relative links and image paths](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#relative-links-and-image-paths-in-markdown-files))

## 安装入口怎么写

安装段落只负责把读者带到“环境已就绪”，不要同时塞入全部配置和业务教程：

1. 先列会阻断安装的前置条件，再开始步骤；GitHub Docs 要求把完成任务所需的前置条件和概念信息放在步骤之前。([Procedural steps](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide#procedural-steps))
2. 只给一条默认安装路径。其他操作系统、网络环境或高级配置用相对链接下沉，避免复制并打断主流程。([Quickstart content type](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type#how-to-write-a-quickstart))
3. 用编号列表表示顺序，每一步至少包含一个明确动作；命令使用代码块。([Procedural steps](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide#procedural-steps))
4. 结尾给一个可观察的成功信号，再给安装变体和故障排查入口。Quickstart 应预先说明将完成什么，并用代码块或其他视觉线索帮助读者确认操作正确。([Quickstart content type](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type#how-to-write-a-quickstart))

最小结构：

```markdown
## 安装

前置条件：...

1. 执行唯一推荐的安装动作。
2. 运行最小检查命令。

成功时应看到：...

[其他环境](docs/installation.md) · [故障排查](docs/troubleshooting.md)
```

## 快速开始怎么写

可直接借用 GitHub 官方 Quickstart 内容模型：让已经准备尝试项目的人，用必要步骤完成一个离散、聚焦的任务；官方给出的目标量级约为五分钟或 600 词，更复杂的任务应转为教程。([Quickstart content type](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type))

1. 开头一句写清受众、前置条件和完成后将得到什么。([How to write a quickstart](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type#how-to-write-a-quickstart))
2. 只演示一条最常见的成功路径；解释“为什么”、完整参数和替代方案用链接下沉。([How to write a quickstart](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type#how-to-write-a-quickstart))
3. 每一步给动作和必要命令，紧接一个短的预期结果，不把故障排查穿插进主流程。([Procedural steps](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide#procedural-steps))
4. 结尾简短回顾已完成的结果，并给 2 至 3 个可执行的下一步。([How to write a quickstart](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type#how-to-write-a-quickstart))

## 贡献入口怎么写

README 中的贡献段落只需要回答“是否欢迎或允许贡献、开始前读什么、去哪里提交、谁负责”。详细开发环境、issue/PR 质量要求、分支与提交规则、检查命令和行为约定放入 `CONTRIBUTING.md`。GitHub 官方列出的贡献指南内容包括创建高质量 issue/PR 的步骤、外部文档或行为准则链接，以及社区与行为预期。([Setting guidelines for repository contributors](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors#adding-a-contributingmd-file))

采用 `CONTRIBUTING.md` 还能获得 GitHub 原生发现能力：GitHub 会在创建 issue 或 PR 时、仓库 `contribute` 页面、仓库概览的 Contributing 标签和侧栏展示入口。文件可位于 `.github/`、仓库根目录或 `docs/`；若有多个，优先级依次为 `.github/`、根目录、`docs/`。([About contributing guidelines](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors#about-contributing-guidelines))

## 内容组织与可读性

- **标题可扫描**：标题必须准确描述下方内容，从 H2 开始，按 H2 -> H3 -> H4 递进，不跳级；同级标题保持唯一。([Headers](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide#headers))
- **语言清楚直接**：面向实际受众使用简单、易接近的语言，不假设所有读者技术水平相同，并尽量使用主动语态。([Voice and tone](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide#voice-and-tone))
- **步骤就是动作**：顺序任务使用编号列表，把前置条件放在步骤前，每一步至少写一个动作。([Procedural steps](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide#procedural-steps))
- **链接克制且有去向**：正文只保留完成当前任务所必需的链接；延伸阅读和下一步放在末尾；链接文字应让读者知道将去哪里。([Links](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide#links))
- **利用 GitHub 原生导航**：GitHub 会根据标题自动生成 Markdown 大纲，并为标题生成可直达的章节链接，无需手写一份重复目录。([Auto-generated table of contents](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes#auto-generated-table-of-contents-for-markdown-files))

## 推荐首页骨架

以下骨架是对上述官方职责和内容模型的精炼应用，不是 GitHub 规定的固定模板：

```markdown
# 项目名

一句话说明项目做什么、为谁解决什么问题。

## 安装

必要前置条件 + 唯一推荐路径 + 成功信号。

## 快速开始

一个聚焦任务 + 最少步骤 + 预期结果。

## 常用入口

按读者任务链接到完整文档，不复制长说明。

## 获取帮助

支持渠道 + 故障排查入口。

## 贡献

贡献边界 + CONTRIBUTING.md 入口。

## 维护

负责团队或维护者 + 完整文档入口。
```

## 已核验官方来源

1. [About the repository README file](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)：README 的用途、典型内容、长文档边界、自动目录、章节链接和相对链接。
2. [Quickstart content type](https://docs.github.com/en/contributing/style-guide-and-content-model/quickstart-content-type)：快速开始的目标、长度量级、受众、前置条件、必要步骤、外链和下一步。
3. [GitHub Docs style guide](https://docs.github.com/en/contributing/style-guide-and-content-model/style-guide)：标题、步骤、语言、列表和链接的可读性规则。
4. [Setting guidelines for repository contributors](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors)：`CONTRIBUTING.md` 的内容、位置和 GitHub 自动展示入口。
