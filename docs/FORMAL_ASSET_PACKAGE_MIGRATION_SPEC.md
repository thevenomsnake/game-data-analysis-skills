# Formal Asset Package Migration

## Problem Statement

使用者无法从现有目录判断一个分析问题的完整正式资产。正式 QUERY、运行证据、可视化、验证和 Dashboard 分散在不同类型目录；物理 archive 又混有唯一副本、重复副本和孤本。Query Workspace 保存了完整本地历史，但当前整理流程只能添加语义标签，不能把用户确认的最新有效查询及其全部产物提升成一个可同步的完整资产。

结果是：文件已经进入 Git 不代表受到资产治理，正式化不代表共享目录已经刷新，同步也可能只搬运散文件而没有完整分析闭包。

## Solution

引入 Formal Asset Package，作为正式共享资产的最小生命周期和同步单位。一个 Package 围绕一个分析问题或显式多查询分析组合，保存所有正式版本、精确证据、可复用输出、验证和可选 Dashboard Delivery。

Query Workspace 保持完整、本地和 Git 忽略。新的资产生命周期流程扫描已索引候选，读取本地 Promotion Ledger 跳过未变化且已有决定的候选，为新候选生成可解释的 Promotion Plan，并在用户确认后形成 Package。所有选中 Package 全部成功并刷新共享发现投影后，Collaboration Transport 才能一次提交同步。

现有正式类型目录和物理 archive 全量迁入 Package 模型。迁移不长期双写，不保留旧副本；旧路径通过 migration map 追踪。

## User Stories

1. 作为 SQL 使用者，我希望本地查询历史保持完整且不自动进入 Git，以便保留探索过程而不污染共享资产。
2. 作为 SQL 使用者，我希望系统记住已审核但决定留在本地的候选，以便下次扫描不重复询问。
3. 作为 SQL 使用者，我希望候选内容或产物变化后重新进入审核，以免旧决定错误覆盖新工作。
4. 作为 SQL 使用者，我希望扫描只处理已索引查询和已登记产物，以免缓存和解包文件被误认为资产。
5. 作为 SQL 使用者，我希望看到同逻辑、严格超集、部分重叠和独立问题之间的具体差异，以便做出保留决定。
6. 作为 SQL 使用者，我希望字节相同或同族旧版本得到明确强推荐，但任何状态变化仍由我确认。
7. 作为 SQL 使用者，我希望疑似重复、低价值或严格超集候选逐组确认，以免系统误删有独立价值的分析。
8. 作为 SQL 使用者，我希望最终 Promotion Plan 列出将提升、留在本地和排除的全部候选，以便一次复核完整范围。
9. 作为 SQL 使用者，我希望一个正式资产入口展示查询、结果、Excel、HTML、验证和 Dashboard，以便理解完整交付。
10. 作为 SQL 使用者，我希望 grouped 与 overall 等显式多查询组合可以属于同一个 Package，以便保持真实分析关系。
11. 作为 SQL 使用者，我希望没有 Dashboard 的复用查询也能形成合法 Package，以便 Dashboard 保持可选派生。
12. 作为 SQL 使用者，我希望没有结果但确认有复用价值的查询可以标记为 unverified，而不是伪装成已验证资产。
13. 作为 SQL 使用者，我希望历史 current、history 和 archived 版本继续可追溯，以便审计修正和替代关系。
14. 作为协作者，我希望同步只接收完整 Package，以免只提交 SQL 而漏掉 sidecar、结果或验证。
15. 作为协作者，我希望任一选中 Package 失败时整批不推送，以免主线出现部分迁移状态。
16. 作为协作者，我希望同步回执区分 Package 已同步和共享目录已刷新，以便准确判断外部可见状态。
17. 作为资产消费者，我希望每个 Package 有永久身份和稳定 manifest，以便不依赖目录标题猜测资产关系。
18. 作为资产消费者，我希望旧路径可以通过 migration map 找到新位置，以便完成一次性升级而不保留重复文件。
19. 作为资产消费者，我希望共享 Catalog、Organization 和 Asset Group Registry 在显式整理同步时一起刷新，以便页面立即看到新资产。
20. 作为维护者，我希望 Formal Asset Repository 独占版本分配、路径、manifest、lineage 和原子写入，以便修复一次即可覆盖所有调用者。
21. 作为维护者，我希望 Formalization 只负责 gate、证据和派生编排，以免它维护第二套正式落盘实现。
22. 作为维护者，我希望 Collaboration Transport 从 Package receipt 取得文件闭包，以免目录白名单成为另一套资产模型。
23. 作为维护者，我希望 Shared Asset Read Models 只读取正式事实，以免 Catalog 或 Organization 反向修改源资产。
24. 作为维护者，我希望 Legacy Quarantine 禁止新写入，以便 archive 的职责不会继续扩张。
25. 作为维护者，我希望 archive 中的重复、唯一和来源文件分别迁往明确所有者，以便最终安全移除旧目录。
26. 作为维护者，我希望迁移前获得完整 dry-run 报告，以便发现孤立文件、缺失闭包和身份冲突。
27. 作为维护者，我希望迁移不采用长期双写，以免新旧存储永远漂移。
28. 作为维护者，我希望项目 manifest 只是紧凑 Package 索引，以免再次复制所有版本事实。

## Implementation Decisions

- 建立三层生命周期：Query Workspace、Formal Asset Repository、Shared Asset Read Models。三层之间只能通过显式 Promotion、投影和同步流转。
- Formal Asset Package 使用项目内永久 FA 身份。跨项目 AG 身份仅用于共享导航，不参与物理身份。
- 一个 Package 对应一个分析问题或显式分析组合；共同标题、日志或主题不能自动合并 Package。
- Package manifest 是成员、当前指针和完整 lineage 真源；单资产 sidecar 是版本事实真源；项目 manifest 是紧凑 Package 索引。
- Workspace Store 继续独占本地查询族、不可变版本、状态和登记产物。
- Workspace Curation 生成候选关系和 Promotion Plan，不直接修改 SQL、生命周期或正式存储。
- Promotion Ledger 与语义 Organization 分离。Ledger 绑定 SQL、结果和登记产物的组合指纹，并记录用户原话、决定、理由和重新审核条件。
- 未登记的工作文件、缓存和解包目录不进入资产审核；需要提升的文件必须先成为 Workspace 的受管理成员。
- Formal Asset Repository 是唯一正式写入模块，负责 FA 身份、Package 路径、版本分配、状态转换、manifest、证据闭包、索引投影和原子落盘。
- Formalization Service 只负责 SQL 规范化、规则与性能 gate、结果证据、Validation 和 Dashboard 派生编排；它不再自行实现正式存储。
- Asset Lifecycle 提供最高行为接口：生成扫描结果、构建 Promotion Plan、dry-run apply、正式 apply 和迁移计划。
- Collaboration Transport 只消费 Package receipt、共享配置和共享投影；不再根据任意目录名推断正式资产。
- Shared Asset Read Models 依次从 Formal Asset Repository 生成 Catalog、Organization 和 Asset Group Registry，不参与 Promotion 判断。
- 显式“整理并同步”执行顺序为：扫描、逐组确认、最终计划确认、应用 Ledger、形成全部 Package、验证、刷新共享投影、构建同步计划、一次提交推送。
- 任一步失败都不得产生可推送的部分结果。Package 内写入原子；整批同步在所有 Package 成功后才开放。
- 默认 Promotion 要求当前 SQL 可运行、无阻断且经用户确认具有共享价值。精确结果存在时必须携带全部登记产物。
- 无结果但确有价值的 QUERY 只能经明确确认进入 unverified Package。Dashboard 一旦存在就必须满足完整证据和验证合同。
- 历史迁移保留 current、history 和 archived 状态；状态不改变物理位置。
- 迁移器按强 lineage、同族版本和显式分析 bundle 建 Package。无法确定 Package 归属时生成用户决策项，不依赖标题或 SQL 相似度自动合并。
- 旧 archive 中与 Package 或正式证据字节相同的副本在哈希验证后移除；唯一有价值资产进入 Package；尚未完成正式化评估的本地资产进入 Workspace 并记 `deferred`（旧迁移输入中的 `keep_local` 只作为 legacy action）；来源材料进入明确的来源、知识或规则证据所有者；未决项保留在只读 Legacy Quarantine。
- 全量切换不长期双写。迁移完成后移除旧类型目录写入逻辑，并输出版本化 path migration map。
- 现有未提交的索引和同步修改不能直接作为最终实现提交；其中“正式索引与 Workspace 分离”“archive 禁止新增”方向可复用，但必须改为 Package 驱动，不能继续扩展目录白名单。

## Testing Decisions

- 最高验收 seam 是 Asset Lifecycle 的 plan/apply 行为接口。
- 使用一个项目内小型 fixture 完成一次端到端 smoke：Workspace 候选、Promotion Plan、Ledger、Package、migration map、共享投影和 Collaboration plan。
- Smoke 只验证外部可观察结果：文件闭包、状态、receipt、索引和同步允许范围，不断言内部 helper 调用。
- Formal Asset Repository 仅补覆盖 Package 原子落盘、身份稳定、版本状态和失败零部分写入的定向合同。
- Collaboration Transport 仅补覆盖“完整 Package 可同步、Workspace 和散文件阻断”的定向合同。
- 迁移器使用代表性 fixture 覆盖同族历史、显式多查询 bundle、重复 archive、唯一 archive 和未决 quarantine。
- 本轮不采用 TDD，不连接真实 GitLab，不运行浏览器，不扩张为全量回归或多阶段质量门禁。
- 实现完成后只运行一次包含上述场景的聚焦 smoke；若失败，只修本次合同范围。

## Out of Scope

- 删除或压缩 Query Workspace 中的本地历史、`_working`、缓存和未登记文件。
- 自动判断疑似重复 SQL 的业务等价性。
- 修改 SQL 业务逻辑、Canonical Rules、Knowledge 或 Dashboard 指标合同。
- 重新设计外部资产网站的视觉界面。
- 将 Query Workspace、Promotion Ledger 或本地处置决定同步到 Git。
- 为旧目录维护永久兼容副本、链接或双写。
- 在普通 QUERY、Formalize 或保存流程中隐式刷新跨项目共享目录。

## Further Notes

- example 当前有 211 个 Workspace 查询族、432 个版本和约 1.06 GB 本地文件；本次迁移只处理正式共享资产和经过确认的 Promotion，不整理本地空间。
- example 当前 manifest 登记 129 个正式 SQL 版本，三件套完整；迁移重点是保持 lineage 和 Package 闭包，而不是修复缺失 SQL。
- 物理 archive 同时含唯一同步副本、重复副本和孤本，必须先完成逐项去向计划，不能直接删除。
- 当前跨项目 Catalog 和 Group Registry 是周期快照。显式“整理并同步”将它们纳入同一用户流程，但仍保持只读投影身份。
