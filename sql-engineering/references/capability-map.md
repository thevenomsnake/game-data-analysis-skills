# SQL Capability Map

This file is generated from `references/capabilities.json`. Edit the registry, then regenerate this file; do not maintain a second capability list by hand.

## Common Actions

| Action | Routed functions | Purpose |
|---|---|---|
| 检查开发库数据 | `DEV_SQL_INSPECT` | 检索历史证据，或只读查看开发库表结构、字段枚举和小范围诊断结果。 |
| 查数据 / 修改 SQL | `QUERY`、`QUERY_EXECUTE` | 生成、修改并保存查询 SQL；需要执行时只消费准确 receipt，并使用公开配置的本地适配器或 Chrome 插件。 |
| 固化 SQL | `SQL_FORMALIZE` | 把已跑通 SQL 和结果证据固化为正式 QUERY，并按需生成 Dashboard。 |
| 可视化 SQL 结果 | `RESULT_VISUALIZATION` | 准确 SQL 返回结果后，Bug 排查默认生成自包含 HTML，传统分析默认生成 Excel，并绑定准确结果 lineage。 |
| 整理结果与可视化 | `RESULT_LINEAGE_ORGANIZATION` | 定期识别结果、Excel 和可视化的准确来源；LLM 必须讲清关键差异，再逐项由用户确认。 |
| 审查 SQL | `REVIEW` | 从产品和代码两个视角审查原始 SQL。 |
| 查找 SQL | `QUERY`、`SQL_REPOSITORY` | 按用途、指标、日志和筛选查找临时历史或正式资产。 |
| 浏览全部资产 | `ASSET_CATALOG`、`ASSET_ORGANIZATION` | 按显式定期任务生成共享资产全状态只读目录，排除本地 query workspace，增量整理业务语义，并为分析问题分配永久 AG 编号和主页目录。 |
| 整理临时 SQL | `QUERY_WORKSPACE_MAINTENANCE`、`RESULT_EVIDENCE_MAINTENANCE` | 定期分类、查重并动态浏览 query workspace；原始结果超过 10 MB 时切片，完整保留可视化，不改 SQL 或生命周期。 |
| 管理口径 | `RULES` | 查看或经明确授权写入项目 canonical rules。 |
| 查询 / 管理资料库 | `KNOWLEDGE` | 优先从项目 active bindings 发现并只读查询映射、枚举、QA/Owner/负责人等可变参考信息；显式授权后才登记、刷新或绑定资料。 |
| 初始化 / 扫描资料目录 | `SOURCE_WORKSPACE` | 配置本机未确认代码、TLOG 文档或外部参考目录，生成候选文件索引，并选择一个准确来源供后续 KNOWLEDGE 审查。 |
| 配置 / 同步策划表 | `PLANNING_SOURCE` | 选择本地自管或远程托管策划源，按产品和阶段检查、同步并封存精确版本，维护项目 source release 绑定。 |
| 开始配置 / 修改配置 | `PROJECT_ADMIN` | 检查尚未配置的项目项，让用户逐阶段确认、复制或停用内网/DA 数据服务绑定，并持久化策划源、本机凭据和本地身份；也支持显式修改已有配置。 |
| 同步资料 | `COLLABORATION_SUBMIT` | 生成本地安全提交计划；不自动推送，不读取生产项目或结果。 |

## Capability Contract

| Function | Mode | Visibility | Entry points | LLM policy | Quality profile | Output |
|---|---|---|---|---|---|---|
| 【项目管理】 `[PROJECT_ADMIN]` | `PROJECT_ADMIN` | `common` | `workspace_setup.py status|sync`<br>`data_service.py status|init|copy|bind|disable|resolve`<br>`local_setup.py status|configure`<br>`planning_source.py status|configure`<br>`sql_project.py init|show-config|set-config|rebuild-index`<br>`project_validate.py`<br>`migrate_*.py` | `none` | `project_admin` | 产品阶段与数据服务绑定状态；配置清单与缺失项；持久化本机配置；项目配置；manifest/index；迁移或健康报告 |
| 【SQL Skill进化】 `[SKILL_EVOLUTION]` | `SKILL_EVOLUTION` | `advanced` | `write_scope_guard.py`<br>`sql-engineering source/tests/references` | `engineering_change_with_regression_tests` | `skill_source_change` | skill source change；regression tests；runtime sync |
| 【口径管理】 `[RULES]` | `RULES` | `common` | `sql_project.py show-rules|rule-report|add-rule`<br>`rule_review.py`<br>`rule_dictionary.py`<br>`rule_authorization_governance.py` | `optional_semantic_structuring` | `canonical_rule_write` | canonical rule；口径审查或字典 |
| 【来源/XML同步】 `[SOURCE_INTAKE]` | `SOURCE_INTAKE` | `advanced` | `xml_catalog.py` | `none` | `source_intake` | xml_catalog.json；项目来源证据 |
| 【候选资料目录】 `[SOURCE_WORKSPACE]` | `SOURCE_INTAKE` | `common` | `source_workspace.py configure|list|scan|select` | `none_for_discovery_optional_for_later_knowledge_review` | `local_only_unconfirmed_no_implicit_promotion` | 本机 source root 配置；候选资料索引；selected_not_reviewed 选择凭据 |
| 【策划源空间】 `[PLANNING_SOURCE]` | `SOURCE_INTAKE` | `common` | `planning_source.py configure|status|check|sync|validate|history` | `none_for_source_revision_semantic_review_only_for_new_projection` | `exact_svn_revision_or_embedded_folder_relative_project_binding` | planning_source_release_v2；planning_source_binding_v2；SVN revision/目录 diff；本机同步配置 |
| 【资料库查询与管理】 `[KNOWLEDGE]` | `KNOWLEDGE` | `common` | `config_knowledge.py register|refresh|bind|resolve|validate|list`<br>`rule_mapping_knowledge_migration.py` | `semantic_contract_during_intake_only` | `immutable_source_versioned_projection_explicit_binding` | 来源快照；资料投影版本；使用契约；项目绑定；knowledge_reference_v1；knowledge_usage_v1 |
| 【需求判定】 `[REQUIREMENT_INTAKE]` | `REQUIREMENT_INTAKE` | `integration` | `requirement_intake.py` | `none` | `read_only_rule_aware_intake` | requirement_intake_v2 JSON；blocking business decisions |
| 【开发库只读检查】 `[DEV_SQL_INSPECT]` | `DEV_SQL_INSPECT` | `common` | `dev_sql_inspect.py ping|tables|describe|enum|query|history|migrate-history` | `summarize_local_result_only` | `readonly_bounded_development_inspection` | 本地 query.sql；本地 result.csv；dev_sql_inspection_receipt_v2；dev_sql_inspection_index_v2 |
| 【查询SQL】 `[QUERY]` | `QUERY` | `common` | `query_window.py`<br>`sql_execution_adapter.py route|render`<br>`sql_summary_planner.py plan|create-bundle`<br>`sql_query_workspace.py save|receipt`<br>`sql_project.py rule-context|resolve-table` | `single_generation_with_bounded_diagnostic_hypothesis_coverage` | `runnable_query_broad_first_for_diagnostics` | query_workspace/vNNN.sql；request_envelope_v1；rule_application_v1；summary_feasibility_v1；可选 query_analysis_bundle_v1；ready query_delivery_receipt_v1；meta/seed/index；精确版本派生产物 |
| 【SQL固化】 `[SQL_FORMALIZE]` | `SQL_FORMALIZE` | `common` | `sql_formalize.py` | `at_most_one_semantic_repair` | `formal_query` | 正式 QUERY；formal_sql_delivery_receipt_v1；run evidence；可选 validation/dashboard；FormalizeBundle；request-bound rule_application_v1 |
| 【结果可视化】 `[RESULT_VISUALIZATION]` | `VALIDATION` | `common` | `self-contained diagnostic HTML + sql_query_workspace.py attach-output`<br>`Spreadsheets skill`<br>`validate_style.py`<br>`sql_result_visualization.py refresh-values|bind|attach-bundle-result|bind-bundle` | `usage_class_routed_html_or_spreadsheet_design_then_deterministic_binding` | `exact_sql_result_presentation_lineage` | 自包含 Bug 排查 HTML 或完整 visualized .xlsx；单结果或 exact_results 证据绑定；HTML attachment receipt 或 sql_result_visualization receipt |
| 【结果资产关系整理】 `[RESULT_LINEAGE_ORGANIZATION]` | `PROJECT_ADMIN` | `common` | `result_lineage_organizer.py inspect|apply` | `deterministic_evidence_then_llm_difference_explanation_then_user_confirmation` | `confirmed_result_lineage_and_lifecycle` | result_lineage_inspection_v1；result_lineage_decision_v1；result_lineage_apply_receipt_v1 |
| 【查询工作台整理】 `[QUERY_WORKSPACE_MAINTENANCE]` | `PROJECT_ADMIN` | `advanced` | `query_workspace_maintenance.py scan|apply|serve` | `semantic_curation_on_demand` | `overlay_only_no_lifecycle_mutation` | organization overlay；duplicate/health scan；dynamic workspace viewer |
| 【结果证据整理】 `[RESULT_EVIDENCE_MAINTENANCE]` | `PROJECT_ADMIN` | `advanced` | `result_evidence_maintenance.py scan|compact|refresh` | `none` | `result_preview_slice_reusable_visual_full` | result_evidence_retention_v1；derived_output_content_revision_v1；结果切片；历史结果整理报告 |
| 【查询验证】 `[VALIDATION]` | `VALIDATION` | `advanced` | `sql_formalize.py --target query-dashboard`<br>`sql_project.py save-run` | `none` | `validation` | run evidence；VALIDATION artifact |
| 【看板SQL】 `[DASHBOARD]` | `DASHBOARD` | `advanced` | `sql_formalize.py --target query-dashboard` | `none` | `dashboard_delivery` | DASHBOARD SQL/spec/meta；DA 契约 |
| 【SQL审查】 `[REVIEW]` | `REVIEW` | `common` | `sql_review.py`<br>`sql_review_subagent_orchestrator.py` | `product_semantic_closure_cached` | `sql_review` | product/code Markdown；sql_review.json/html |
| 【SQL仓库】 `[SQL_REPOSITORY]` | `PROJECT_ADMIN` | `advanced` | `sql_repository.py build|serve` | `none` | `persisted_facts_only` | sql_repository.json/html |
| 【全量资产目录】 `[ASSET_CATALOG]` | `PROJECT_ADMIN` | `integration` | `asset_catalog.py build|validate` | `none` | `all_shared_assets_status_descriptive_only` | sql_asset_catalog_v2 JSON |
| 【资产生命周期整理】 `[ASSET_LIFECYCLE]` | `PROJECT_ADMIN` | `integration` | `asset_lifecycle.py scan|inventory|closeout-plan|plan|apply` | `deterministic_scan_user_confirmed_apply` | `workspace_closeout_v2` | asset_lifecycle_scan_v2；workspace_unregistered_inventory_v1；asset_lifecycle_closeout_plan_v1；Promotion Ledger v2；Formal Asset Package receipt |
| 【资产提供方快照】 `[ASSET_PROVIDER_SNAPSHOT]` | `PROJECT_ADMIN` | `integration` | `asset_provider.py build|validate` | `none` | `formal_asset_provider_read_only_snapshot` | asset_provider_snapshot_v1；asset_provider_manifest_v1；asset_provider_snapshot.json；asset_provider_manifest.json；snapshot digest；read-only consumer contract |
| 【全量资产语义整理】 `[ASSET_ORGANIZATION]` | `PROJECT_ADMIN` | `integration` | `asset_organization.py scan|refresh|apply|validate`<br>`asset_group_registry.py scan|refresh|validate` | `optional_new_or_changed_assets_only` | `semantic_overlay_no_source_mutation` | sql_asset_organization_v2 JSON；sql_asset_group_registry_v2 JSON；增量待整理清单；稳定 AG 主页目录 |
| 【看板HTML审查】 `[DASHBOARD_REVIEW_HTML]` | `REVIEW` | `advanced` | `dashboard_review.py build|serve|mark` | `none` | `dashboard_review` | dashboard_review.json/html/state |
| 【中间表】 `[INTERMEDIATE_TABLE]` | `INTERMEDIATE_TABLE` | `advanced` | `sql_project.py save-table|update-table|table-report` | `optional_planning` | `intermediate_table` | 中间表元数据和构建 SQL |
| 【项目健康检查】 `[PROJECT_HEALTH]` | `PROJECT_ADMIN` | `advanced` | `project_validate.py` | `none` | `read_only_health` | current/full health result；grouped summary JSON；full audit JSON |
| 【跨项目口径审查】 `[RULE_REVIEW]` | `RULES` | `advanced` | `rule_review.py` | `none` | `rule_review` | rule review JSON/HTML/state |
| 【口径字典】 `[RULE_DICTIONARY]` | `RULES` | `advanced` | `rule_dictionary.py` | `none` | `persisted_facts_only` | rule_dictionary.json/html |
| 【执行查询SQL】 `[QUERY_EXECUTE]` | `QUERY` | `integration` | `Chrome plugin`<br>`sql_query_workspace.py receipt|attach-output|mark` | `exact_receipt_serial_single_active_fill_submit_download_close_then_bind` | `serial_one_active_query_tab_download_bound_cleanup` | exact result evidence or manual handoff |
| 【协作提交计划】 `[COLLABORATION_SUBMIT]` | `PROJECT_ADMIN` | `common` | `collaboration_submit.py plan|submit` | `none` | `local_review_only` | public_collaboration_plan_v1 |

## Reference Routing

- `ASSET_CATALOG`: context=sql-projects 根目录; references=`asset-catalog.md`.
- `ASSET_LIFECYCLE`: context=项目根目录或 sql-projects 根目录；明确的扫描、dry-run 或用户确认; references=`query-workspace.md`、`project-workflow.md`、`formal-asset-package.md`.
- `ASSET_ORGANIZATION`: context=sql_asset_catalog_v2；可选的 LLM 或人工分类决策; references=`asset-organization.md`、`asset-groups.md`、`asset-catalog.md`.
- `ASSET_PROVIDER_SNAPSHOT`: context=sql-projects 根目录；稳定 repository_id; references=`asset-catalog.md`、`../docs/READONLY_ASSET_CONSUMER_GUIDE.md`.
- `COLLABORATION_SUBMIT`: context=repository root；current worktree; references=`project-workflow.md`.
- `DASHBOARD`: context=项目；正式 QUERY；验证状态；明确的 DA 控件需求; references=`dashboard-review.md`、`spec-contracts.md`.
- `DASHBOARD_REVIEW_HTML`: context=项目根目录；已保存 Dashboard; references=`dashboard-review.md`.
- `DEV_SQL_INSPECT`: context=项目阶段及已确认 development_inspection 服务；表或日志；检查字段或只读 SQL；有界日期范围; references=`data-services.md`、`dev-sql-inspection.md`、`operating-contract.md`.
- `INTERMEDIATE_TABLE`: context=项目；用途；粒度；来源；刷新策略; references=`intermediate-tables.md`.
- `KNOWLEDGE`: context=目标项目；查询用途或资料管理请求；资料来源或已审核导出规格（仅登记/刷新时）; references=`knowledge-management.md`.
- `PLANNING_SOURCE`: context=SQL 项目；产品与阶段；本地自管路径或远程托管 SVN URL；明确的管理方式; references=`planning-source.md`、`knowledge-management.md`、`operating-contract.md`.
- `PROJECT_ADMIN`: context=项目 slug 或项目根目录; references=`setup-onboarding.md`、`data-services.md`、`local-setup.md`、`planning-source.md`、`operating-contract.md`、`project-workflow.md`.
- `PROJECT_HEALTH`: context=项目根目录; references=`project-workflow.md`.
- `QUERY`: context=项目；业务问题；用户时间范围或可解析的项目默认窗口；输出粒度/指标，或诊断主体与有界证据范围; references=`project-workflow.md`、`sql-taxonomy.md`、`core-rules.md`、`query-workspace.md`、`execution-routing.md`、`time-integrity.md`、`dialects/<selected-execution-profile>.md`.
- `QUERY_EXECUTE`: context=ready query delivery receipt；用户自己的 Chrome 登录态（仅在明确选择浏览器适配器时）; references=`project-workflow.md`.
- `QUERY_WORKSPACE_MAINTENANCE`: context=项目根目录; references=`project-workflow.md`、`query-workspace.md`.
- `REQUIREMENT_INTAKE`: context=用户原文；可选项目根目录；可选的当前澄清文本; references=`operating-contract.md`.
- `RESULT_EVIDENCE_MAINTENANCE`: context=项目根目录; references=`project-workflow.md`、`query-workspace.md`、`asset-catalog.md`.
- `RESULT_LINEAGE_ORGANIZATION`: context=项目根目录；确定性检查证据；逐项语义差异说明；用户逐项确认; references=`result-lineage-organization.md`、`result-visualization.md`、`query-workspace.md`.
- `RESULT_VISUALIZATION`: context=项目根目录；准确 SQL 路径或 query_analysis_bundle_v1；对应返回结果文件；可选展示要求或明确跳过原因; references=`result-visualization.md`、`project-workflow.md`、`time-integrity.md`.
- `REVIEW`: context=项目角色；SQL 或批次路径；可选结果文件; references=`sql-review.md`、`sql-review-design-record.md`、`sql-review-product-agent.md`.
- `RULES`: context=项目；口径内容或 concept_key; references=`rule-manager.md`、`rule-context-event-signatures.md`.
- `RULE_DICTIONARY`: context=sql-projects 根目录; references=`rule-manager.md`.
- `RULE_REVIEW`: context=sql-projects 根目录; references=`rule-manager.md`.
- `SKILL_EVOLUTION`: context=明确的 skill 改造请求；失败案例或目标行为; references=`sql-workflow-optimization-record.md`.
- `SOURCE_INTAKE`: context=项目；XML/TLOG 或来源证据; references=`source-index.md`.
- `SOURCE_WORKSPACE`: context=仓库根目录；本机来源目录或已配置 root_id；可选项目范围; references=`source-workspace.md`、`knowledge-management.md`、`operating-contract.md`.
- `SQL_FORMALIZE`: context=项目根目录；源 SQL；结果文件；固化目标；用户确认; references=`project-workflow.md`、`performance-routing.md`、`sql-header-layering.md`、`spec-contracts.md`、`time-integrity.md`.
- `SQL_REPOSITORY`: context=项目根目录; references=`project-workflow.md`.
- `VALIDATION`: context=项目；正式 QUERY；结果证据; references=`spec-contracts.md`.

## Protected Writes

| Scope | Accepted functions | Reason |
|---|---|---|
| `skill_source` | `SKILL_EVOLUTION` | SQL generation and project workflows must treat source/runtime skill files as read-only. |
| `canonical_rules` | `RULES` | Canonical rules and their change log require a separately authorized RULES request. |
| `knowledge_assets` | `KNOWLEDGE` | Knowledge snapshots, projections, usage contracts, and project bindings require an explicit knowledge-management request. |
| `planning_sources` | `PLANNING_SOURCE`、`PROJECT_ADMIN` | Complete planning releases and project source bindings require the planning-source or setup workflow. |

## Command Gates

| Script / command | Accepted functions |
|---|---|
| `config_knowledge.py *` | `KNOWLEDGE` |
| `config_knowledge.py list` | `KNOWLEDGE`、`QUERY`、`REVIEW`、`SQL_FORMALIZE`、`DASHBOARD`、`PROJECT_HEALTH` |
| `config_knowledge.py resolve` | `KNOWLEDGE`、`QUERY`、`REVIEW`、`SQL_FORMALIZE`、`DASHBOARD` |
| `config_knowledge.py validate` | `KNOWLEDGE`、`PROJECT_HEALTH`、`PROJECT_ADMIN` |
| `source_workspace.py *` | `SOURCE_WORKSPACE` |
| `source_workspace.py list` | `SOURCE_WORKSPACE`、`SOURCE_INTAKE`、`KNOWLEDGE`、`PROJECT_ADMIN` |
| `planning_source.py *` | `PLANNING_SOURCE` |
| `planning_source.py configure` | `PLANNING_SOURCE`、`PROJECT_ADMIN` |
| `planning_source.py status` | `PLANNING_SOURCE`、`PROJECT_ADMIN`、`KNOWLEDGE` |
| `planning_source.py check` | `PLANNING_SOURCE`、`PROJECT_ADMIN`、`KNOWLEDGE` |
| `planning_source.py validate` | `PLANNING_SOURCE`、`PROJECT_ADMIN`、`KNOWLEDGE`、`PROJECT_HEALTH` |
| `planning_source.py history` | `PLANNING_SOURCE`、`PROJECT_ADMIN`、`KNOWLEDGE` |
| `local_setup.py *` | `PROJECT_ADMIN` |
| `data_service.py *` | `PROJECT_ADMIN` |
| `workspace_setup.py *` | `PROJECT_ADMIN` |
| `dashboard_review.py *` | `DASHBOARD_REVIEW_HTML`、`REVIEW` |
| `migrate_legacy_sql_work.py *` | `PROJECT_ADMIN` |
| `migrate_query_workspace.py *` | `PROJECT_ADMIN` |
| `migrate_canonical_rule_store.py *` | `SKILL_EVOLUTION`、`RULES`、`PROJECT_ADMIN` |
| `rule_activation_governance.py *` | `SKILL_EVOLUTION`、`RULES` |
| `rule_activation_governance.py audit` | `SKILL_EVOLUTION`、`RULES`、`PROJECT_ADMIN` |
| `rule_mapping_knowledge_migration.py *` | `SKILL_EVOLUTION`、`RULES`、`KNOWLEDGE` |
| `rule_authorization_governance.py *` | `SKILL_EVOLUTION`、`RULES` |
| `query_workspace_maintenance.py *` | `QUERY_WORKSPACE_MAINTENANCE` |
| `query_workspace_maintenance.py apply` | `QUERY_WORKSPACE_MAINTENANCE`、`PROJECT_ADMIN` |
| `result_evidence_maintenance.py *` | `RESULT_EVIDENCE_MAINTENANCE` |
| `result_evidence_maintenance.py compact` | `RESULT_EVIDENCE_MAINTENANCE`、`PROJECT_ADMIN` |
| `result_evidence_maintenance.py refresh` | `RESULT_EVIDENCE_MAINTENANCE`、`PROJECT_ADMIN` |
| `query_window.py *` | `QUERY`、`REQUIREMENT_INTAKE`、`PROJECT_ADMIN` |
| `sql_execution_adapter.py *` | `QUERY`、`SQL_FORMALIZE`、`REVIEW`、`PROJECT_ADMIN` |
| `sql_execution_adapter.py route` | `QUERY`、`SQL_FORMALIZE` |
| `sql_execution_adapter.py render` | `QUERY`、`SQL_FORMALIZE` |
| `sql_execution_adapter.py inspect` | `QUERY`、`SQL_FORMALIZE`、`REVIEW`、`PROJECT_ADMIN` |
| `requirement_intake.py *` | `REQUIREMENT_INTAKE` |
| `dev_sql_inspect.py *` | `DEV_SQL_INSPECT` |
| `rule_review.py *` | `RULE_REVIEW`、`RULES` |
| `sql_formalize.py *` | `SQL_FORMALIZE`、`VALIDATION`、`DASHBOARD` |
| `sql_formalize_seed.py *` | `QUERY`、`SQL_FORMALIZE` |
| `sql_project.py *` | `PROJECT_ADMIN` |
| `sql_project.py init` | `PROJECT_ADMIN` |
| `sql_project.py set-config` | `PROJECT_ADMIN` |
| `sql_project.py add-rule` | `RULES` |
| `sql_project.py save-sql` | `QUERY`、`VALIDATION`、`DASHBOARD` |
| `sql_project.py update-artifact` | `PROJECT_ADMIN`、`QUERY`、`VALIDATION`、`DASHBOARD` |
| `sql_project.py save-table` | `INTERMEDIATE_TABLE` |
| `sql_project.py update-table` | `INTERMEDIATE_TABLE` |
| `sql_project.py save-note` | `PROJECT_ADMIN`、`RULES`、`SOURCE_INTAKE`、`REQUIREMENT_INTAKE`、`QUERY`、`SQL_FORMALIZE`、`VALIDATION`、`DASHBOARD`、`REVIEW`、`SQL_REPOSITORY`、`DASHBOARD_REVIEW_HTML`、`INTERMEDIATE_TABLE`、`PROJECT_HEALTH`、`RULE_REVIEW`、`RULE_DICTIONARY` |
| `sql_project.py save-run` | `VALIDATION`、`DASHBOARD` |
| `sql_project.py rebuild-index` | `PROJECT_ADMIN` |
| `sql_project.py describe-sql-write-formalize-seed` | `QUERY`、`SQL_FORMALIZE` |
| `sql_query_workspace.py *` | `QUERY` |
| `sql_query_workspace.py init` | `PROJECT_ADMIN` |
| `sql_query_workspace.py upgrade-contract` | `PROJECT_ADMIN` |
| `sql_query_workspace.py receipt` | `QUERY`、`QUERY_EXECUTE` |
| `sql_query_workspace.py attach-output` | `QUERY`、`QUERY_EXECUTE`、`RESULT_VISUALIZATION` |
| `sql_query_workspace.py mark` | `QUERY`、`QUERY_EXECUTE` |
| `sql_summary_planner.py *` | `QUERY` |
| `sql_summary_planner.py plan` | `QUERY` |
| `sql_summary_planner.py create-bundle` | `QUERY` |
| `sql_result_visualization.py *` | `RESULT_VISUALIZATION` |
| `sql_result_visualization.py migrate` | `RESULT_VISUALIZATION`、`PROJECT_ADMIN`、`SKILL_EVOLUTION` |
| `validate_style.py *` | `RESULT_VISUALIZATION`、`SKILL_EVOLUTION` |
| `result_lineage_organizer.py *` | `RESULT_LINEAGE_ORGANIZATION` |
| `result_lineage_organizer.py inspect` | `RESULT_LINEAGE_ORGANIZATION`、`PROJECT_ADMIN`、`SKILL_EVOLUTION` |
| `result_lineage_organizer.py apply` | `RESULT_LINEAGE_ORGANIZATION`、`PROJECT_ADMIN`、`SKILL_EVOLUTION` |
| `sql_repository.py *` | `SQL_REPOSITORY` |
| `asset_catalog.py *` | `ASSET_CATALOG` |
| `asset_lifecycle.py *` | `ASSET_LIFECYCLE` |
| `asset_lifecycle.py scan` | `ASSET_LIFECYCLE`、`PROJECT_ADMIN` |
| `asset_lifecycle.py inventory` | `ASSET_LIFECYCLE`、`PROJECT_ADMIN` |
| `asset_lifecycle.py closeout-plan` | `ASSET_LIFECYCLE`、`PROJECT_ADMIN` |
| `asset_lifecycle.py plan` | `ASSET_LIFECYCLE`、`PROJECT_ADMIN` |
| `asset_lifecycle.py apply` | `ASSET_LIFECYCLE`、`PROJECT_ADMIN` |
| `asset_provider.py *` | `ASSET_PROVIDER_SNAPSHOT` |
| `asset_provider.py validate` | `ASSET_PROVIDER_SNAPSHOT`、`PROJECT_HEALTH` |
| `asset_organization.py *` | `ASSET_ORGANIZATION` |
| `asset_group_registry.py *` | `ASSET_ORGANIZATION` |
| `sql_review.py *` | `REVIEW` |
| `xml_catalog.py *` | `SOURCE_INTAKE` |
| `collaboration_submit.py *` | `COLLABORATION_SUBMIT` |
| `collaboration_submit.py plan` | `COLLABORATION_SUBMIT` |
| `collaboration_submit.py status` | `COLLABORATION_SUBMIT` |
| `collaboration_submit.py submit` | `COLLABORATION_SUBMIT` |
