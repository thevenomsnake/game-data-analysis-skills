# 专业数据报告视觉风格：三家机构官方材料对照

> 文档类型：研究笔记
>
> 适用范围：Excel 分析工作簿、管理层数据报告
>
> 最近验证：2026-07-21

## 结论边界

本文比较 Goldman Sachs、Morgan Stanley 与 JPMorgan Chase 的官方年报、投资者材料和研究图表。严格区分：

- **官方明示**：材料直接写出的标题、单位、指标定义、来源、日期和脚注；
- **观察模式**：官方成品中可见的排版与编码，不等同于公开品牌规范；
- **Excel 转译**：本文为分析工作簿提出的实现建议，不代表三家机构要求。

所查材料未公开可复用的完整色值、字体规范，也未提供足够证据证明使用了 Excel 式“单元格内数据条”。因此，色彩只讨论相对层级；数据条只作为有条件的 Excel 转译，不列为机构惯例。

## 已核验官方来源

### Goldman Sachs（保留原核验集）

1. [Goldman Sachs 2025 Annual Report](https://www.goldmansachs.com/investor-relations/financials/current/annual-reports/2025-annual-report)：页面图表“Significant Growth Across Key Metrics Since Our Investor Day 2020”“Firmwide Net Revenues”“Medium-Term AWM Targets”。
2. [官方图表：Significant Growth](https://www.goldmansachs.com/investor-relations/financials/current/annual-reports/2025/SignificantGrowth.jpg)：2019/2025 对比、总股东回报和脚注标记。
3. [官方图表：Firmwide Net Revenues](https://www.goldmansachs.com/investor-relations/financials/current/annual-reports/2025/FirmwideNetRevenues.jpg)：柱上总额、柱内组成值、图例和脚注标记。
4. [The Global Economy Is Forecast to Post 'Sturdy' Growth of 2.8% in 2026](https://www.goldmansachs.com/insights/articles/the-global-economy-forecast-to-post-sturdy-growth-in-2026)：研究图表标注 Goldman Sachs Research，数据截至 2025-12-18。
5. [Goldman Sachs 2024 Annual Report](https://www.goldmansachs.com/investor-relations/financials/current/annual-reports/2024-annual-report)：用于核查章节化叙事和跨版式连续性。

### Morgan Stanley 与 JPMorgan Chase（新增 4 条）

6. [Morgan Stanley 4Q 2025 Strategic Update](https://www.morganstanley.com/content/dam/msdotcom/en/about-us-ir/shareholder/4q2025-strategic-update.pdf)：官方投资者材料；用于比较分组指标、趋势、目标和脚注的紧凑呈现。
7. [Morgan Stanley Global Midyear Investment Outlook 2025](https://www.morganstanley.com/insights/articles/investment-outlook-midyear-2025)：官方 Insights；用于比较研究叙事、情景/资产类别组织和时间口径。
8. [JPMorgan Chase 2025 Annual Report](https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/annualreport-2025.pdf)：官方年报；用于比较多级财务表、分部数据、总计与注释体系。
9. [J.P. Morgan 2026 Market Outlook](https://www.jpmorgan.com/insights/global-research/outlook/market-outlook)：官方 Global Research 页面；含 2026 情景概率、季度货币对预测和大宗商品价格预测官方图表资产，并声明信息以材料所示日期/时间为准。

## 官方明示

- **Goldman Sachs**：图表直接给出标题、单位、来源、截至日期与定义性脚注；年报图表显式披露 Apple Card transition 等调整项。[来源 1–4]
- **Morgan Stanley**：投资者材料和 Insights 以报告期或展望期组织指标与观点；指标目标、比较期间及限定条件随对应板块呈现。[来源 6–7]
- **JPMorgan Chase / J.P. Morgan**：年报以正式表头、期间、分部和总计组织披露；Global Research 页面把预测期写入图表主题，并声明信息以材料标示日期/时间为准且不承诺更新。[来源 8–9]
- 三家所查材料均未明示其成品色值、字体或表格样式可作为外部品牌模板；本文不提供“官方色号”或“官方 Excel 模板”。

## 观察到的对照模式

| 维度 | Goldman Sachs | Morgan Stanley | JPMorgan Chase / J.P. Morgan | 可迁移共性 |
|---|---|---|---|---|
| 色彩层级 | 白/浅中性底；深色总量或主系列，低饱和色区分组成与调整项。 | 中性底和深色正文为主，强调色集中在关键趋势、类别或目标。 | 年报表格以中性层级为主；研究资产用有限类别色区分情景、货币对或商品。 | 先用明度、字重和位置建层级，再用少量颜色表达语义；不要把每个类别都染成强色。 |
| 分组/分布 | 组成值置于柱内，总额置于柱顶；起点-终点对比紧凑。 | 业务/资产类别按区块或同一尺度成组，趋势与目标贴近对应指标。 | 年报采用多级列头、分部行和跨期列；研究图把同类预测组织成紧凑比较组。 | 分组名称先于明细；同组共享单位、小数位和尺度，避免重复标签。 |
| 紧凑幅度编码 | 可见柱内组成、柱顶总额和深色总量线；不是单元格数据条。 | 可见紧凑趋势/比较图；本次核验不足以确认单元格数据条。 | 可见情景概率和预测比较图；本次核验不足以确认单元格数据条。 | Excel 数据条仅在长列表需要快速排序感知时使用，并保留数字；不可据此声称复刻机构样式。 |
| 总计/Overall | 总额直接标注，Total 使用更深、更粗的视觉权重。 | 整体结果与分项/业务指标并置，整体层级高于组成项。 | 财务表以 Total/合计行、上边界和字重收束分部；研究图以总体情景或目标值形成锚点。 | 总计行用加粗、上边框和一致数字格式；避免整行高饱和填色。 |
| 注释 | 调整项、定义和脚注贴近图表底部。 | 指标定义、目标口径和前瞻限定随相关板块出现。 | 年报注释体系更正式、可追溯；研究页面另有完整时点与免责声明。 | 异常、估算、预测和口径变化必须文字化，不只靠颜色或星号。 |
| 来源 / as-of | 图内直接出现 Research 来源和精确数据截至日。 | 报告期/展望期在标题和材料上下文中明确。 | 年报明确报告期；研究页面明确“截至材料所示日期/时间”并说明更新责任。 | 每个输出区块固定保留“来源 + 数据截至 + 报告生成时间”；预测另标预测期。 |

## Excel 转译（本文建议）

1. **适配可复用 Excel 的阅读顺序**：稳定的对象/指标标题 -> 单位、Base 与观察期 -> 可选观察注释 -> 分组表或图 -> 有意义的整体统计 -> 来源/截至日期 -> 脚注。机构研究材料可以使用编辑型结论标题，但参数可刷新工作簿应把结论降为可选注释，避免标题随一次结果失效。
2. **建立三层色彩**：正文/总量用最深中性色，主系列用一个克制强调色，比较项用低饱和色或灰；调整项同时使用标签或纹理，不能只换颜色。
3. **分组表优先结构**：组名行或多级列头、缩进明细、统一数值格式；用留白和细横线分组，减少全网格。有序区间保留业务顺序，无序类别可按主指标排序，并明确样本、区间或分母。
4. **整体统计必须有信息量**：加总型指标可在组尾使用 Total/Overall；平均值、率、去重值和分位数应按自身口径重算。分布列已归一为 100% 时不重复显示无信息量的总计行，优先展示精确整体均值或分位数。
5. **把数据条作为 Excel 微型图**：8 行以上、同量纲、非负且需快速比较时可使用。单列分布使用一个主色；同量纲矩阵可以覆盖全部指标单元格，但必须共享一个全局尺度。保留精确数字，使用纯色无描边条，排除整体统计行；混合正负、跨量纲时不用。
6. **可审计注释**：一次性调整、预测、估算、定义变化分别编号，并紧邻对应表/图；推荐统一格式：`Source: ... | Data as of: YYYY-MM-DD | Report generated: YYYY-MM-DD HH:mm TZ`。

## 最小检查表

- 分组、明细和有意义的整体统计是否有稳定且不同的视觉层级？
- 删除颜色后，标签、位置、字重和线型是否仍足以解释结果？
- 数据条若存在，是否保留数字且没有跨量纲比较？
- 底部整体统计是否真的增加信息，而不是重复 100%；均值、率和分位数是否按正确口径重算？
- 来源、数据截至日、预测期和必要脚注是否与对应输出同屏？
