# 阶段九：运营管理台管理控制台设计

> 状态：已批准设计，等待实施计划与分任务审查
> 日期：2026-09-04
> 项目路径：`D:\E-commerce_operations`
> 上游：[总设计](2026-08-25-ecommerce-operations-design.md)、[知识库与 Milvus 混合检索设计](2026-08-27-knowledge-rag-design.md)、[商品优化与合规设计](2026-08-28-product-optimization-compliance-design.md)、[人工修订、审批与模拟发布设计](2026-08-31-manual-review-approval-publish-design.md)、[运营管理台前端设计](2026-09-01-frontend-operations-console-design.md)

## 1. Goal and non-goals

阶段九完成总设计中的管理控制台边界：

- 管理员可在浏览器管理既有知识文档、版本状态和停用操作；所有角色可在授权店铺上下文内使用既有知识检索。
- 固定、离线、无真实模型调用的 Agent 评测可由受控本地 CLI 持久化到 PostgreSQL，主管和管理员可查看安全结果，浏览器不能发起评测写入。
- 主管和管理员可按授权范围读取安全的 Agent 调用摘要与完整的只追加审计记录。
- 管理员可管理现有用户的角色、启停状态、店铺授权，以及现有店铺的启停状态。
- Vue 管理台增加“知识库”“Agent 评测”“审计日志”“系统管理”四个导航模块，并沿用既有任务工作台和审批流程。

本阶段不做以下事项：

- 不接入真实电商平台，不创建真实发布、价格、SKU 或库存修改流程，也不新增价格专员角色。
- 不在浏览器、HTTP API 或后台 Worker 中触发真实模型评测；不读取真实 DeepSeek Key，不下载模型，不调用外网。
- 不做真实模型评测、故障注入、完整真实服务浏览器 E2E、最终评测报告或最终文档核对；这些均保留给阶段十。
- 不创建微服务、独立评测服务、状态管理库、图表库、缓存层或新依赖；继续使用 FastAPI、PostgreSQL、Vue 3、Element Plus、现有 `frontend/src/api.ts` 与现有单体部署方式。
- 不新增知识文档下载、删除、重新启用、原文浏览、向量浏览或本地路径浏览功能。

## 2. Approved decisions and architecture

阶段九在既有单体中增加受控读模型和少量管理写模型，而不是重做已验证的工作流、知识 Worker、审批或认证边界。

```text
管理员知识操作 ──> 既有 FastAPI 知识 API ──> PostgreSQL + 既有知识 Worker
授权店铺检索 ──> 既有 /knowledge/search ──> 既有本地检索链路

固定离线 fixture ──> 受控本地 CLI ──> evaluation_runs/results
                                                    │
Vue 管理控制台 ──> 既有 api.ts ──> 只读评测 / Agent 调用 / 审计 API
                                                    │
管理员用户/店铺操作 ──> 锁定事务 ──> users/stores/scopes + audit_events
```

`agent_calls` 继续是实际工作流调用的安全可观测事实，不替代评测运行或评测结果。`evaluation_runs` 是运行级事实头，`evaluation_cases` 是版本化 fixture，`evaluation_results` 是逐用例证据；浏览器只读取这些事实，不保存或触发任一评测。

现有 `session.ts`、`api.ts`、Vue Router、Element Plus、`/app` 同源托管与 `sessionStorage` JWT 保持不变。`api.ts` 仍是唯一网络边界：受保护 GET 使用 `cache: "no-store"`，所有页面通过其统一处理安全 HTTP 错误。前端路由、按钮与移动端显示只是体验控制；服务端每次请求重新加载用户、角色、店铺状态、范围和资源归属。

## 3. Roles, store scope, and authorization

### 3.1 Authoritative role matrix

| 能力 | operator | supervisor | admin |
|---|---:|---:|---:|
| 在授权且启用的店铺上下文中检索知识 | 是 | 是 | 是 |
| 上传、创建版本、停用知识文档 | 否 | 否 | 是 |
| 查看本店范围的 Agent 评测和调用摘要 | 否 | 是 | 是，所有范围 |
| 查看审计 | 否 | 是，授权店铺 | 是，全部记录 |
| 管理用户角色、用户状态和店铺范围 | 否 | 否 | 是 |
| 管理店铺启停状态 | 否 | 否 | 是 |
| 提交、审批自己的方案 | 保持既有规则 | 保持既有自审规则 | 保持既有自审规则 |

`UserStoreScope` 继续是 `operator` 和 `supervisor` 的店铺限制事实。`admin` 对全部店铺具有管理和只读可见性，不依赖是否存在单独的 `UserStoreScope` 行；所有查询路径必须统一这一规则，不能出现认证辅助函数允许管理员、但工作台、审计、审批或新管理读模型因无 scope 行而返回空结果的分裂行为。

知识文档本身仍是全局演示资源，不按店铺复制或分区。为保持“授权店铺知识搜索”边界，既有 `POST /knowledge/search` 请求增加必填 `store_id`：服务端先确认当前用户、该店铺启用状态和实时 scope，再运行既有全局 active-document 检索。`store_id` 只用于鉴权与审计上下文，不能让用户选择、枚举或改写其他店铺资源。

所有资源读取都先确认当前用户有效，再在同一查询中应用店铺范围和资源链；越权或跨店 ID 不泄露资源存在性。对 supervisor，评测、调用和审计只显示当前 `UserStoreScope` 可见的店铺记录；全局记录只对 admin 可见。被禁用用户得到既有认证失败，不得到旧会话的读取权。

### 3.2 User and store safety invariants

管理员用户变更必须在单一事务内重新锁定当前 actor、目标用户以及按 `id` 稳定排序的全部启用 admin 用户行。事务在写入前和写入后都验证：

1. actor 仍为启用的 admin；
2. actor 不可禁用自己；
3. actor 不可将自己的角色从 `admin` 移除；
4. 变更后的启用 admin 数量至少为一。

因此两个管理员并发互相禁用、并发移除最后两个 admin 角色，或在同一目标上并发更新，都不能产生零个启用 admin。违反时不写用户、scope 或审计，返回稳定 `ADMIN_GUARD_VIOLATION` 或 `ADMIN_CONFLICT`，不返回内部数据库细节。

`PUT /admin/users/{user_id}/store-scopes` 是完整替换而非增量 patch。事务锁定 actor、目标 user、当前 scope 行和按 ID 稳定排序的目标店铺行；拒绝重复、未知或禁用店铺 ID，然后一次性删除旧 scope、写入请求中的精确集合并提交。失败或并发冲突回滚整个替换，不留下半集合。禁用用户或店铺不删除历史 workflow、Agent 调用、评测、审计、提案或发布事实；禁用只阻止未来认证、操作或新资源使用。

店铺停用与用户停用同样保留历史。停用店铺不再可作为新搜索、工作流或 scope 写入目标；admin 仍可读取其历史审计和评测，supervisor 只在仍有该历史 store scope 时读取相应历史。现有 supervisor/admin 自审批准语义不因阶段九角色管理而改变。

## 4. Data model, migration, and constraints

阶段九使用一个位于 `0005_manual_review_approval_publish` 之后的 Alembic revision。迁移不改写既有业务数据、不重新解释 `workflow_runs`、`AgentCall`、proposal revision 或审批 iteration 语义。

### 4.1 Fixed evaluation facts

已批准的初始评测数据草案只有 `evaluation_cases` 与 `evaluation_results`。本设计保留的 `evaluation_runs` 是唯一额外评测实体：它是表达一次运行的版本/范围/终态元数据、保存失败但零逐用例结果的运行、并为稳定 `GET /agent-evaluations/runs` 列表提供事实头所必需。它不是独立服务、在线评测能力或浏览器写入口，也不引入其他评测聚合、队列或执行实体。

`evaluation_cases` 保存受版本控制的固定离线用例，而非浏览器表单输入。

| 列 | 约束与用途 |
|---|---|
| `id` | `String(36)` 主键。 |
| `agent_type` | 闭集：`analysis`、`optimization`、`compliance`、`knowledge_retrieval`。 |
| `case_key` | `String(64)`，同 agent 类型内的稳定用例名。 |
| `case_version` | 正整数，代表不可变 fixture/预期的版本。 |
| `fixture` | 非空 JSON，只含合成可信事实、固定检索 query 或验证输入；不得含完整 Prompt、原始文档、路径、凭据或 provider 内容。 |
| `expected` | 非空 JSON，只含可确定验证的期望；不得含 provider 原始输出。 |
| `enabled` | 布尔值；关闭的用例不进入新运行。 |
| `created_at`、`updated_at` | UTC 时间戳。 |

数据库约束为：`case_version >= 1`、`fixture`/`expected` 非空、`agent_type` 闭集，以及 `UNIQUE(agent_type, case_key, case_version)`。用例版本只新增，不覆盖已被结果引用的版本。

`evaluation_runs` 是一次不可变的、单 agent 类型的离线执行头。它包含 `id`、`agent_type`、可空 `store_id`、`suite_version`、`runner_version`、`dataset_version`、`execution_mode='offline_fixture'`、`status`、`started_at`、`completed_at`、安全 `summary`、安全 `error_code`、可空 `created_by` 与 `created_at`。

- `analysis`、`optimization`、`compliance` 运行必须有 `store_id`；`knowledge_retrieval` 可以是全局运行，因此 `store_id` 可空且只对 admin 可见。
- `status` 只能是 `completed` 或 `failed`；两种终态都必须有 `completed_at >= started_at`。运行创建后不更新状态、摘要或结果。
- `summary` 只允许 `total_cases`、`passed_cases`、`failed_cases`、`average_latency_ms` 和与该 `agent_type` 对应的已批准聚合 metrics；未知 key、非有限数、负时延或超出定义区间的比例在写入前拒绝。
- 索引至少为 `(agent_type, created_at, id)`、`(store_id, created_at, id)` 与 `(status, created_at, id)`，用于稳定时间倒序列表后以 `id` 打破并列。

`evaluation_results` 是不可变的逐用例结果：`id`、`evaluation_run_id`、`evaluation_case_id`、`agent_type`、`outcome`、`metrics`、`result_code`、`latency_ms` 与 `created_at`。它有两个外键、`latency_ms >= 0`、`outcome IN ('passed', 'failed')`、非空 JSON metrics，以及必须存在的：

```text
UNIQUE(evaluation_run_id, evaluation_case_id, agent_type)
```

服务层在写入同一事务中确认 run、case 与 result 的 `agent_type` 精确相同；这一跨行关系不能只依赖前端。一个成功运行先写完整 header 和所有启用 case 的结果，再一起提交；CLI 失败只写一个安全 `failed` run，不留下半组 results。相同 fixture 的再次执行创建新的 run，不更新旧 run。`result_code` 是最长 64 字符的闭集：`EVALUATION_PASSED`、`EVALUATION_EXPECTATION_MISMATCH`、`EVALUATION_INPUT_INVALID`、`EVALUATION_VALIDATION_FAILED` 或 `EVALUATION_RUNNER_FAILED`；不能存异常文本或模型文本。

允许的逐类型 metrics 是闭集：

| `agent_type` | 允许 metrics |
|---|---|
| `analysis` | `candidate_set_valid`、`rank_order_valid`、`latency_ms` |
| `optimization` | `output_schema_valid`、`trusted_fact_valid`、`citation_valid`、`latency_ms` |
| `compliance` | `deterministic_valid`、`semantic_schema_valid`、`citation_valid`、`latency_ms` |
| `knowledge_retrieval` | `recall_at_10`、`mrr`、`citation_document_version_accuracy`、`latency_ms` |

布尔项只能为布尔，比例只能为有限 `0..1` 数字，时延只能为非负有限数字。API 只返回这些 allowlisted metrics、短码、case key/version 和运行元数据；不返回 `fixture`、`expected` 或原始中间数据。

### 4.2 Audit compatibility and append-only policy

既有 `audit_events` 继续保存现有人工修订、审批、模拟发布和授权拒绝事实。迁移扩展它以容纳阶段九全局管理事件，而不重写旧行：

- `store_id` 允许为空，空值仅表示全局管理或全局评测/知识事件；旧 store-scoped 行保持原值。
- 增加成对的 `resource_type` 与 `resource_id`：二者要么同时为空，要么同时存在。`resource_type` 闭集为 `user`、`store`、`knowledge_document`、`knowledge_version`、`evaluation_run`；`resource_id` 为 `String(36)`。
- 增加 `knowledge_document_created`、`knowledge_version_created`、`knowledge_document_disabled`、`evaluation_run_persisted`、`admin_user_updated`、`admin_user_scopes_replaced` 与 `admin_store_updated` 到 event type 闭集；现有 event type、FK 和记录不改变。
- 对新查询增加 `(event_type, created_at, id)`、`(actor_id, created_at, id)` 与 `(outcome, created_at, id)` 索引，保留现有 store、proposal、workflow 相关索引。

审计仍是只追加的。成功的知识、用户、scope、店铺与离线评测持久化在同一事务中写入一条事件；失败的已开始事务回滚后不写成功事件，受控拒绝可写现有 `authorization_denied` 事件。所有细节必须通过服务器 allowlist 和 canonical UTF-8 JSON（最多 4096 bytes）校验。除既有安全字段外，阶段九只允许：`from_role`、`to_role`、`from_user_status`、`to_user_status`、`scope_count`、`store_enabled`、`document_status`、`evaluation_agent_type`、`evaluation_status`、`case_count`、`changed_fields`。每个字段有闭集 enum、长度、布尔、非负整数或有限数字约束；未知 key 和未可序列化值在 `session.add()` 前拒绝。

审计 details 绝不包含请求体、密码哈希、Idempotency-Key、hash、异常 repr、评论全文、文档正文、fixture、Prompt、provider 内容、向量、路径、连接信息或凭据。

### 4.3 Migration safety and downgrade

升级必须在 PostgreSQL 中保持既有行可读，先扩展 enum/check、列和索引，再创建评测表与外键。新约束不得要求回填旧 `AgentCall`、旧 audit 或既有用户/店铺记录。

降级只在没有阶段九事实时允许：若任一 `evaluation_cases`、`evaluation_runs` 或 `evaluation_results` 存在行，或存在阶段九新增 event type 的 audit 行，downgrade 以稳定迁移错误拒绝，绝不静默删除评测或审计事实。没有这些事实时才按依赖反序撤销新表、索引、列和新增枚举/check 范围。该策略保护历史且不假装数据可逆。

## 5. APIs and response boundaries

所有新增 path ID 是 `1..36` 字符，分页统一 `page >= 1`、`page_size 1..100`；所有请求模型 `extra='forbid'`。响应使用现有安全错误处理：401 表示无有效当前用户，403 表示已认证但角色不允许，404 不泄露跨店资源，409 表示并发或状态冲突，422 只表示输入边界，5xx 只返回稳定安全码和 request ID。

### 5.1 Knowledge management and search

阶段九复用既有下列接口与生命周期：

```text
POST /knowledge/documents
GET  /knowledge/documents
POST /knowledge/documents/{document_id}/versions
POST /knowledge/documents/{document_id}/disable
POST /knowledge/search
```

新增只读 `GET /knowledge/documents/{document_id}/versions?page&page_size`，仅 admin 使用。它返回 document ID/name/category/enabled、版本 ID/number/status/parser/chunker/embedding version、错误短码和时间戳；不返回原始文件名、文件正文、storage path、lease owner、chunks 或向量。它补足管理台的安全生命周期视图，不新增下载、删除或重新启用动作。

`POST /knowledge/search` 保持既有 envelope、质量状态与检索语义，但请求加入必填 `store_id` 并在查询前应用实时店铺鉴权。结果只显示受控 canonical chunk 片段、文档名、版本、chunk ID、类别、分数和质量；管理台绝不显示整份文件、服务器路径、向量、模型权重位置或内部错误。`zero_hit`、`low_confidence`、timeout 和 dependency error 均显示为不同的真实状态，不伪造答案或结果。

### 5.2 Evaluation and Agent-call reads

浏览器只有以下只读评测与调用接口：

```text
GET /agent-evaluations/runs?page&page_size&agent_type&status&store_id
GET /agent-evaluations/runs/{run_id}
GET /agent-calls?page&page_size&store_id&workflow_type&node_name&status&error_code
```

`GET /agent-evaluations/runs` 返回安全 run list：ID、agent type、可见 store ID、版本标识、offline execution mode、终态、开始/完成时间、allowlisted summary 与安全 error code。`GET /agent-evaluations/runs/{run_id}` 在相同授权过滤之后返回该 run 的安全元数据和结果列表；每条结果只含 case key/version、agent type、outcome、allowlisted metrics、短码和时延。不存在、不可见或跨店 run 均返回安全 404。operator 统一 403；supervisor 仅能读取授权店铺结果；admin 能读取全部 store-scoped 和 global 结果。

`GET /agent-calls` 通过 `AgentCall -> WorkflowRun -> Store` 的资源链实时授权。它只返回 `workflow_run_id`、store/workflow type、node name、call type、iteration、attempt、model、prompt version、status、token 汇总、duration、可选 estimated cost、安全 error code 和时间戳。它绝不返回 `input_hash`、workflow input/output、请求 headers、Prompt、response、思维链、checkpoint、租约或 provider 诊断。supervisor 仅看授权店铺；admin 看全部；operator 403。

评测持久化没有 HTTP 写接口。受控本地 CLI 是唯一写入入口，要求显式本地写入开关和 `--write-results`，并仅使用版本化 fixture、fake loader 与 MockTransport。没有开关时只计算并输出本地安全汇总，不写数据库；不开真实 provider、RAG 模型、Milvus 或网络。它不会读取 API Key，也不会把原始输入或结果输出到日志、数据库或 shell，也不创建独立评测服务、在线任务或浏览器写模型。

### 5.3 Audit reads

既有 `GET /audit-events` 保留 `store_id`、`proposal_id`、`action`、分页和稳定排序，并增加安全、可索引的可选筛选：`workflow_run_id`、`event_type`、`actor_id`、`outcome`、`created_from` 与 `created_to`。时间范围必须为有效 UTC 时间、起点不晚于终点、范围最长 31 天。没有全文搜索、details JSON 查询、自由文本、错误文本或任意排序参数。

列表按 `created_at DESC, id DESC` 稳定分页。supervisor 的查询强制加入当前 scope，且忽略 global 事件；admin 可查看所有 store-scoped、disabled-store 历史与 global 事件。响应仍只包含关系 ID、安全 enum、allowlisted details、短码、request ID 和时间戳，不暴露任何内部快照或敏感载荷。

### 5.4 Admin management writes and reads

```text
GET   /admin/users?page&page_size&role&status&store_id
PATCH /admin/users/{user_id}
PUT   /admin/users/{user_id}/store-scopes
GET   /admin/stores?page&page_size&enabled
PATCH /admin/stores/{store_id}
```

所有这些接口仅允许当前启用 admin。`GET /admin/users` 只返回 `id`、`username`、`role`、`status`、`created_at` 和安全的 `store_ids`；绝不返回 password hash、JWT、认证时间、Cookie 或登录失败细节。`PATCH /admin/users/{user_id}` 接受至少一个 `role` 或 `status` 的完整目标值，且无额外字段。`PUT /admin/users/{user_id}/store-scopes` 接受唯一、长度受限的 `store_ids` 数组，表示替换后的完整集合。`GET /admin/stores` 返回稳定只读的 `id`、`name`、`code`、`enabled`、`created_at`；`PATCH /admin/stores/{store_id}` 只接受完整目标值 `{"enabled": boolean}`，不得接受 `name`、`code` 或 `id`。

管理员 mutation 均使用上文的锁定事务并在成功状态写入同一事务内追加安全 audit event。重复相同 PUT/PATCH 只保持请求的目标状态，绝不产生重复 scope 或删除历史；每次被接受的管理动作仍保留自己的只追加 audit 事实。并发版本冲突或受保护 admin 违反返回稳定 409。服务端不会因为前端已禁用按钮而跳过这些检查。

## 6. Frontend information architecture and responsive behavior

阶段八现有“工作台、经营分析、优化任务、审批中心”保持原路由与行为。阶段九新增四个导航模块，不出现无功能的导航项目：

| 模块 | 路由 | 可见角色 | 内容 |
|---|---|---|---|
| 知识库 | `/app/knowledge` | operator、supervisor、admin | 授权店铺搜索；admin 额外看到文档、版本和停用管理。 |
| Agent 评测 | `/app/agent-evaluations` | supervisor、admin | 安全 run 列表、run 详情和安全调用摘要入口；无“运行评测”按钮。 |
| 审计日志 | `/app/audit-events` | supervisor、admin | server-side filters、分页和 text-bound safe details。 |
| 系统管理 | `/app/admin` | admin | “用户与权限”“店铺启停”两个页签。 |

页面继续使用 Element Plus 表格、筛选器、抽屉、表单和 status tag；不引入图表库、全局状态库或第二套组件体系。评测以数字、状态表格和明细抽屉展示，审计 details 以字段化文本展示，所有服务端文本通过 Vue text binding 渲染，禁止 `v-html`。

桌面与平板提供完整阶段九功能，沿用现有紧凑表格与抽屉重排。手机不显示以上四个管理导航，也不显示其中任何写操作。已登录用户直接访问这些管理 deep link 时，路由在发出页面数据请求之前显示明确的“此管理功能仅支持桌面或平板”设备限制视图；不请求知识管理、评测、调用、审计或 admin API。未登录用户仍先进入登录流程，角色不允许的用户仍显示既有 403 视图。现有手机只读任务查看和 supervisor/admin 审批能力保持不变。

每个新列表区分 loading、无数据、筛选无结果、403、404、409、422 和未知错误；筛选条件在加载失败时保留。写表单在提交期间禁用重复点击，成功后重新读取服务端事实。用户/店铺的危险启停操作要求明确确认，但确认框不替代后端事务保护。所有页面使用可见 label、键盘可操作控件、文字加颜色的状态和 `aria-live="polite"` 状态更新。

## 7. Security, privacy, and error codes

Phase 9 新增或扩展的 API、CLI 输出、评测事实、日志、审计、前端状态、路由参数和 UI 都不得保存、返回、格式化或显示以下敏感内容。既有内部 `KnowledgeChunk.canonical_text` 与 `WorkflowRun` 事实不改变，但绝不复制到阶段九的新读模型或展示面：

- API Key、密码、password hash、JWT、Cookie、Authorization、连接信息或任意 secret；
- 完整 Prompt、原始 provider request/response、思维链、异常堆栈、异常 repr；
- 完整知识文档正文、上传本地路径、原始文件、向量、模型权重路径；
- workflow input/output、checkpoint、lease owner/expiry、Idempotency-Key、请求 hash 或 input hash；
- 未 allowlist 的 audit JSON、评测 metrics、自由错误对象、发布快照或跨店资源细节。

安全错误使用封闭、可展示的代码，例如 `ADMIN_GUARD_VIOLATION`、`ADMIN_CONFLICT`、`ADMIN_USER_NOT_FOUND`、`ADMIN_STORE_NOT_FOUND`、`ADMIN_STORE_DISABLED`、`EVALUATION_RUN_NOT_FOUND`、`AGENT_CALL_NOT_FOUND`、`AUDIT_FILTER_INVALID` 和既有 `KNOWLEDGE_*` 代码。内部 SQL、外部服务和文件系统细节只留在受控本地诊断路径，不传播给浏览器或评测事实。

## 8. Offline evaluation lifecycle

固定离线评测以仓库版本控制的 synthetic fixtures 为输入。CLI 对每个启用 case 构造可信、最小输入，调用确定性 validator 或 MockTransport 驱动的现有客户端边界，收集闭集 metrics，并按既有 `knowledge_evaluation.py` 的确定性排序和指标计算原则生成结果。它不改生产 Prompt、不需要新的真实 smoke，也不扩张已有 Agent retry 语义。

运行顺序为：加载固定 case 版本 → 检查显式持久化开关 → 创建内存结果 → 完成每个 case 的闭集验证 → 单事务写入 immutable run 与全部 results → 写一条安全 audit event。任一步事实、schema、事务或 allowlist 校验失败时，先回滚这笔成功写入事务；再以独立事务写入安全 `failed` run（零 results）。若该失败记录事务也不能提交，则不持久化任何评测事实并向 CLI 返回失败。不会重试真实 provider，不会将失败假装成通过，也不会写部分 case 结果。

新评测运行不是在线质量承诺。阶段九只展示固定、可重放的离线证据；阶段十才可在单独授权下评估真实模型、故障注入和完整端到端表现。

## 9. Test matrix and acceptance criteria

普通单元与前端 fixture 测试全部离线：清空 DeepSeek、知识集成与真实 smoke opt-in，使用 SQLite/fake、MockTransport、固定 fixture 和内存前端数据。不加载本地模型、不下载模型、不访问网络、不执行真实 provider。本机 PostgreSQL 是必须通过的集成门禁，通过显式 `RUN_POSTGRES_INTEGRATION=1` 启用，且同样不需要网络、DeepSeek、模型下载或真实 provider。

| 层级 | 必须覆盖 |
|---|---|
| 迁移与模型 | 评测三表、FK、closed enum、JSON allowlist、唯一 `(run, case, agent type)`、索引、旧行兼容；无阶段九事实时 downgrade 可逆，有事实时明确拒绝。 |
| 离线 CLI | 固定 case 的确定性结果、禁用 case 排除、每次新 run 不覆盖历史、成功原子完整写入、失败无半 results、无网络/Key/provider。 |
| 评测与调用 API | 匿名 401、operator 403、supervisor 仅授权 store、admin 全部、global run 仅 admin、分页/筛选边界、跨店/未知 ID 不泄露、安全字段闭集。 |
| 审计 | 既有 store/proposal/action 行为回归；新增 filters、稳定排序、disabled-store 历史、global-event admin 可见性、safe details 拒绝、无敏感内容。 |
| Admin API | 角色/状态 schema、scope 的原子替换、重复/未知/禁用 store 拒绝、禁用后历史保留、每次成功 mutation 一条安全 audit、self-disable/self-demotion/零 enabled-admin 与并发写入防护。 |
| 知识管理 | admin 管理、非 admin 拒绝 mutation、scope-validated search、文档/版本安全列表、zero-hit/low-confidence/error 区分、无原文下载/路径/向量泄露。 |
| 前端单元 | 四导航的角色与设备显示、mobile deep-link 零管理 API 请求、loading/empty/filtered-empty/error/403 状态、filters/paging、文本绑定、admin 安全确认。 |
| 前端浏览器 fixture | desktop/tablet 使用知识、评测、审计和系统管理的可见路径；mobile 限制；仅确定性 API fixture，不称为真实服务 E2E。 |
| 本机 PostgreSQL opt-in | 在不启用知识/DeepSeek/真实 smoke 且不访问外网的本地 PostgreSQL 中，以 `RUN_POSTGRES_INTEGRATION=1` 执行 Alembic upgrade/downgrade：验证新表、FK、CHECK/enum、JSON allowlist、唯一约束和索引实际生效；验证无阶段九事实时 downgrade 成功、有事实时稳定拒绝；使用两个独立事务/连接复现并发最后 admin 禁用或降级并始终保留至少一个 enabled admin；验证 scope 完整替换、管理 mutation 与 audit、成功 evaluation header/results 与 audit、失败零-result run 的关键事务原子性。 |
| 最终门禁 | 全量 pytest、前端 typecheck/test/build、compileall、本机 PostgreSQL 上的 Alembic upgrade/downgrade/current/check、docker compose config、git diff --check 与干净 Git 状态。 |

阶段九最终验收必须同时包含普通离线门禁和上述本机 PostgreSQL opt-in 门禁；仅 SQLite 或静态 Alembic `check` 不足以证明迁移、约束、并发保护和事务语义。验收完成时，管理控制台只能陈述：本地知识管理、固定离线评测持久化、安全调用/审计可见性、用户权限与店铺启停管理及 fixture 浏览器链路已经按验证证据完成。它不得陈述真实平台发布、真实模型评测、故障注入或完整真实服务浏览器 E2E 已完成。

## 10. Compatibility and deferred Phase 10 work

本阶段不改变现有分析、optimization、manual review Worker 的 graph、lease、checkpoint、RAG 路径、Prompt、DeepSeek client、自动 iteration 上限、审批事务、价格/SKU/库存只读约束或 supervisor/admin 自审权限。`AgentCall` 的既有唯一键和安全字段保持事实来源；Phase 9 仅为它增加经过 scope 过滤的读模型。

阶段十单独处理真实模型评测、受控故障注入、真实 FastAPI/PostgreSQL/Worker 的完整浏览器 E2E、评测报告、最终 README/架构/简历一致性核对。届时每项外部调用、真实模型或真实集成运行仍需要独立 opt-in、审查和安全输出边界。
