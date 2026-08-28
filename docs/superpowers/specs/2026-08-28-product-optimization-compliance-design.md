# 商品优化与合规阶段设计

> 状态：已批准的阶段四设计，尚未实施  
> 日期：2026-08-28  
> 项目路径：`D:\E-commerce_operations`  
> 上游：[总设计](2026-08-25-ecommerce-operations-design.md)、[持久经营分析 Agent 设计](2026-08-26-durable-analysis-agent-design.md)、[知识库与 Milvus 混合检索设计](2026-08-27-knowledge-rag-design.md)

## 1. 目标与范围

本阶段把已完成的经营分析候选接到一个可恢复、可审计的商品优化闭环：

```text
运营选品 → optimization workflow → 商品优化 Agent
→ 确定性检查 → 合规 Agent → 最多两次自动修订
→ draft_ready 或 pending_manual/degraded
```

它交付的是可读取的方案草稿和合规结论，不是商品事实的写回或发布动作。商品优化 Agent 与合规审核 Agent 都使用 `deepseek-v4-flash`，但各自拥有独立 Prompt、Pydantic 输出模型、LangGraph 节点和 `agent_calls` 审计身份。

本阶段明确不实现人工修订、主管审批、模拟发布、前端、真实平台接入、图片或视频生成。本阶段也不增加 Redis、Celery、Kafka、MCP、微服务、额外 Compose 服务或本地生成式大语言模型。

## 2. 架构与数据流

系统仍是 FastAPI 模块化单体加仓库内独立 Worker。API 路由和业务逻辑只做身份、实时店铺范围、选品事务和只读方案查询；它们不访问、记录、输出或调用 DeepSeek Key，也不调用 DeepSeek。优化 Worker 从 PostgreSQL 领取自己的 `optimization` 运行，以运行 ID 作为 LangGraph `thread_id`，再执行下列固定顺序：

```text
POST /analysis-runs/{id}/select-product
  └─ 单一 PostgreSQL 事务：锁定分析运行与候选
       → analysis completed/product_selected
       → product_proposals
       → accepted optimization workflow_runs

optimization Worker
  └─ load trusted product/candidate facts
       → retrieve active RAG rules with citations
       → optimization Agent
       → server proposal validation + deterministic compliance checks
       → compliance Agent semantic review
       → draft_ready | revision loop | pending_manual/degraded | failed
```

PostgreSQL 是运行、商品事实、候选、方案版本、审核、租约、检查点和审计的事实源。Milvus 仍仅是可重建的 BGE-M3 dense+sparse 索引；正文、标题路径、文档与版本、类别、激活状态和可展示引用均由 PostgreSQL 复核。知识检索保留现有 active-version 过滤、canonical citation、`zero_hit`、`low_confidence`、timeout 与 dependency-error 语义。

本地 RAG 只使用已验证的 D 盘模型、缓存与运行目录；不自动下载模型，不访问模型仓库。运行、缓存和模型优先使用 D 盘，且不得删除、移动或整理 C 盘个人文件。

## 3. 工作流状态机与循环上限

### 3.1 `workflow_runs` 的类型化状态

`workflow_runs.workflow_type` 扩展为 `analysis | optimization`。两个 Worker 的领取查询都必须包含自己的类型：分析 Worker 永不领取 `optimization`，优化 Worker 永不领取 `analysis`。两类任务共用 PostgreSQL 服务器时间、`FOR UPDATE SKIP LOCKED`、租约和 owner guard 模式，但不共用业务处理图。

数据库对日期强制类型化约束：

| 工作流类型 | `start_date` / `end_date` 约束 |
|---|---|
| `analysis` | 两者均非空，且 `start_date <= end_date`。 |
| `optimization` | 两者均为 `NULL`。 |

分析的已有路径保持 `accepted → processing → awaiting_selection → failed`。选品是唯一把可选分析运行推进为 `completed` 的动作，并写入 `current_step=product_selected`。优化运行使用：

```text
accepted → processing → draft_ready
                     ├→ pending_manual/degraded
                     └→ failed
```

`draft_ready`、`pending_manual` 与 `failed` 都不可再领取。`quality_status` 保持 `normal | partial | degraded`；本阶段的自动修订耗尽或外部依赖持续不可用固定为 `pending_manual/degraded`，不把结果误报为正常。

### 3.2 有界迭代

初稿为 iteration `0`，自动修订为 iteration `1` 和 `2`。因此每个优化运行最多产生 3 次商品优化和 3 次合规审核：

```text
iteration 0: optimize → deterministic checks → semantic compliance
iteration 1: optimize → deterministic checks → semantic compliance
iteration 2: optimize → deterministic checks → semantic compliance
```

只有确定性检查与语义审核在同一 iteration 都通过时，才写入 `draft_ready`。任一审核不通过时，服务端仅把已验证的违规项、必需修改和可信引用组成下一次修订输入；不会把原始模型回答或未验证事实带入下一轮。iteration `2` 仍未双轨通过时，终态为 `pending_manual/degraded`，不产生第四次优化或审核。

## 4. 数据模型与数据库约束

### 4.1 `workflow_runs` 与 `agent_calls` 兼容扩展

`workflow_runs` 保留现有 `id`、店铺、创建者、状态、租约、尝试次数、安全输入输出、质量、错误码和时间戳。`optimization` 的安全输入最少含 `proposal_id`、源 `analysis_run_id`、`analysis_candidate_id`、`product_id` 与服务端确定的 `store_id`；它不保存 Prompt、Authorization 或密钥。

`agent_calls` 新增非空整数 `iteration`，默认值为 `0`，范围不小于零。迁移将既有分析调用填充为 `iteration=0`，因此所有已完成分析记录仍可读取。唯一键由：

```text
(workflow_run_id, node_name, call_type, iteration, attempt)
```

替代原键。`attempt` 仍表示单个逻辑调用的传输或 schema-repair 尝试；`iteration` 表示初稿或哪一次自动修订。这样重领、checkpoint 恢复和同一节点重放都不会重复创建调用审计行。

### 4.2 `product_proposals`

每个已选分析候选对应一个方案聚合行，至少包含：`id`、`analysis_run_id`、`analysis_candidate_id`、`optimization_run_id`、`store_id`、`product_id`、`base_product_version`、选品幂等键哈希、当前 revision 引用、创建与更新时间。`analysis_run_id`、`analysis_candidate_id` 和 `optimization_run_id` 各自唯一，确保一个分析运行只能选择一个候选、一个候选只能生成一个方案、一个方案只对应一个优化运行。方案表不保存 lifecycle `status`、`quality_status` 或 `error_code`；这些字段只以关联 optimization `workflow_runs` 为事实源。

创建方案时服务器同时锁定分析运行、候选与商品，并验证候选属于该运行、运行处于 `awaiting_selection`、商品仍属于该店铺。该事务捕获 `products.current_version` 为 `base_product_version`，再将分析运行置为 `completed/product_selected`，写入方案行，并创建唯一的 `optimization/accepted` 运行。任一步失败整笔事务回滚，不留下半个方案或孤立优化运行。`base_product_version` 是选品之后的乐观版本边界：它不声称检测选品前分析期间发生的历史商品变化。

### 4.3 `proposal_revisions`

`proposal_revisions` 是不可变的方案版本。每行至少含 `id`、`proposal_id`、`iteration`、可信 `base_product_version` 与事实引用、经过服务端验证的优化输出、引用集合、创建时间。`(proposal_id, iteration)` 唯一，iteration 仅允许 `0..2`。已保存版本从不更新；修订只能插入后续 iteration。方案的当前 revision 只在同一租约所有者、同一事务中前进，不能倒退。它不重复保存确定性检查结果。

版本中的业务输出只包含下面第 5 节定义的受验证字段。它不包含完整 Prompt、原始响应、隐藏推理、路径、向量、认证头或凭据。

### 4.4 `compliance_reviews`

每个方案版本最多有一行合规审核：`proposal_revision_id` 唯一，另含 `proposal_id`、`iteration`、确定性检查明细、已验证语义审核结论、`passed`、风险级别、required changes、可信引用、安全错误码、质量状态和时间戳。一个 revision 的确定性检查与 LLM 语义审核只保存在这一唯一 `compliance_reviews` 行。审核结论同样不可变；重放按唯一键返回已有行，而不是再次请求模型。

数据库外键保证 proposal、revision、review、analysis candidate、product 和 workflow 的归属链。数值 iteration、建议价格、状态组合和唯一键均由命名的 `CHECK` 与 `UNIQUE` 约束约束；服务层在入库前执行同样的资源归属校验。

## 5. 可信事实、RAG 与方案输出契约

### 5.1 可信输入与引用

优化 Worker 每个 iteration 都从 PostgreSQL 读取当前受授权店铺内的商品、SKU、类目、库存展示事实、选中候选的确定性指标与证据，并在加载前确认 `products.current_version == base_product_version`。模型不能选择任意店铺、商品、SKU 或字段，也不能覆盖这些事实。

RAG 只向优化与合规节点提供现有 active 文档版本的 canonical chunks。每项引用必须带 `document_id`、`version_id`、`chunk_id`、文档名、版本号、类别与 canonical 文本摘要。服务端在写入前确认引用属于 active 版本、规则类别适用于当前商品且引用确实支持所声称的新增属性、建议或合规判断；Milvus 返回的孤儿、停用或非 active chunk 一律丢弃。

没有 active 规则、`zero_hit` 或 `low_confidence` 不可被当作规则已满足。需要规则依据的新增属性、价格建议、SKU 建议或合规放行在这种情况下不能通过确定性检查。

### 5.2 商品优化 Agent 输入与输出

商品优化 Agent 的独立 Pydantic 模型禁止额外字段。输入是最小必要的可信产品和 SKU 当前值、分析候选诊断、上一轮已验证的 required changes，以及 RAG 引用。输出必须为：

| 输出 | 规则 |
|---|---|
| `title` | 中文标题；由确定性长度与禁限词检查覆盖。 |
| `selling_points` | 有序卖点数组；每项可追溯到可信事实或引用。 |
| `description` | 结构化详情文案；不得引入无来源事实。 |
| `keywords` | 搜索关键词数组；不得用禁限词或伪造属性。 |
| `attribute_completions` | 每个新增或补全属性含字段、值、事实或引用依据。 |
| `changes` | 每项含字段、当前值、建议值、理由与证据；当前值必须与服务器快照精确相符。 |
| `citations` | 上述内容所用的有效 RAG 引用。 |
| `price_suggestions` | 每个目标 SKU 含当前价格、建议价格、理由与证据。 |
| `sku_suggestions` | 每个目标 SKU 含当前 SKU 值、建议 SKU 值、理由与证据。 |

价格和 SKU 输出始终是建议，不会更新 `products`、`product_skus`、库存、价格或任何其他商品事实。没有提交、人工修订、审批或发布接口能在本阶段应用这些建议。

服务端拒绝未知 SKU、伪造或不匹配的当前值、无来源属性、非法或非 active 引用、非正建议价格，以及高于或低于当前 SKU 价格 30% 之外的建议。任一被拒绝字段使该 revision 的确定性检查不通过；不以模型自述修复或默认值替代。

### 5.3 合规审核 Agent 输入与输出

合规审核 Agent 的 Prompt、Pydantic 模型、LangGraph 节点名和 `agent_calls.node_name` 与商品优化 Agent 完全分离。它接收已验证的候选 revision、服务器确定性检查结果、可信事实和必要规则引用，不接收未验证原始文本。

其严格输出为 `passed`、`risk_level`、`violations`、`required_changes`、`citations`、`confidence` 与 `degraded`。它不修改方案、商品或 SKU，只返回可验证的语义风险与所需修改。所有引用再由服务器按 active RAG 版本验证。

## 6. 双轨合规与放行规则

每个 revision 在语义审核之前都运行确定性检查，至少覆盖：禁限或受限表述、字段长度、缺失必需商品事实、属性来源、SKU 存在性和当前值、价格正值及 ±30% 范围、引用结构与 active 归属。确定性检查产生稳定的机器可判定违规码和安全可展示说明。

合规 Agent 独立检查夸大、医疗化、误导、语义矛盾、无法证明的承诺和证据不足。两条轨道的任何一条失败都不能 `draft_ready`；确定性检查通过也不替代语义审核，语义 `passed=true` 也不覆盖确定性违规。

同一 revision 的确定性结果和合规审核结论只在其 `compliance_reviews` 行持久化后才决定下一步。若有可修订的违规且尚有 iteration 额度，优化 Agent 只接收经验证的修改清单进入下一次 iteration。无可安全修订内容、额度耗尽或依赖持续失败时进入 `pending_manual/degraded`。

## 7. API、RBAC 与幂等

### 7.1 选品接口

```text
POST /analysis-runs/{id}/select-product
```

请求体只有 `candidate_id`，请求头必须有长度受限且非空的 `Idempotency-Key`。只有当前启用且拥有该运行店铺范围的 `operator` 可以调用；不得依赖 JWT 中过期的角色或范围声明，也不允许 `supervisor` 或 `admin` 调用此接口。

路由在同一数据库事务中重新读取用户状态、角色、店铺范围、分析运行和候选，并锁住分析运行。首次有效选择返回 HTTP `202`，附方案 ID、optimization workflow run ID 与 `accepted` 状态。相同运行的同一候选重放返回 HTTP `200` 及既有资源；无论请求到达顺序如何，都不会创建第二份方案或优化运行。试图把该分析运行改选为不同候选固定返回 HTTP `409` 与 `ANALYSIS_SELECTION_CONFLICT`。候选不存在、候选不属于运行、运行未就绪或无权限均不泄露其他店铺资源。

选品幂等键仅以受控哈希保存用于审计与关联；日志不记录原始请求头值。并发请求由分析运行锁和 `product_proposals` 的唯一约束共同串行化。

### 7.2 只读方案接口

```text
GET /proposals/{id}
```

当前启用的 `operator`、`supervisor` 或 `admin` 仅能读取其当前数据库店铺范围内的方案。每次请求都重新加载用户的启用状态、角色和范围。响应包含安全的方案、不可变 revision、唯一 `compliance_reviews` 合规结果，以及关联 optimization `workflow_runs` 的质量状态和稳定错误码；不返回租约 owner、checkpoint、存储路径、向量、Prompt、原始响应、密钥或 Authorization。

本阶段只有上述两条新增路由。没有方案提交、人工修订、审批、驳回、模拟发布或商品更新接口。

## 8. 租约、checkpoint 与恢复

优化 Worker 与分析 Worker 保持两个独立的领取函数和图，但采用一致的数据库语义。PostgreSQL 领取使用 `FOR UPDATE SKIP LOCKED` 与数据库 `now()`；领取是唯一增加 attempt count 的动作。优化 Worker 只选择自己的 `workflow_type='optimization'`、可领取状态和未耗尽尝试次数的行。

每次数据库状态更新都同时匹配运行 ID、`workflow_type='optimization'`、`status='processing'`、`lease_owner` 和 `lease_expires_at > now()`。每次 DeepSeek 或 RAG 外部调用之前都先续租。Worker 在加载事实、每次持久化 revision，以及写入任一终态前都重新确认 `products.current_version == base_product_version`；若变化则 owner-guard 写入 `failed/PRODUCT_VERSION_CONFLICT`，不得继续使用旧事实。条件更新影响零行即表示失租：旧 Worker 立即停止，不再请求外部依赖、不写 revision、review、agent call 或终态，也不提交。

`workflow_run.id` 固定为优化 LangGraph 的 `thread_id`。checkpoint 只保存安全的结构化业务状态：可信标识、已持久化 revision/review ID、iteration、稳定错误码和下一节点；不保存 Key、Authorization、完整 Prompt、原始响应或思维链。重领后从最后成功 checkpoint 继续，并用 `(proposal_id, iteration)`、`proposal_revision_id`、`agent_calls` 唯一键和 owner guards 判定已做工作，因而不重复 revision、review 或调用审计。

`asyncio.CancelledError` 代表进程中断，不应被通用异常捕获为正常失败。它保留有效 `processing` 租约；在租约到期后由新的 owner 领取并从 checkpoint 安全重放。checkpoint 读取、保存或一致性损坏属于事实层错误，记录安全错误码并进入 `failed`，不会用降级文案掩盖。

## 9. 错误分类与降级

| 失败类别 | 持久化结果 | 运行终态 |
|---|---|---|
| DeepSeek timeout、不可用、限流或持续传输失败 | 保存已完成的确定性检查、失败阶段和安全错误码；不编造文案或引用。 | `pending_manual/degraded` |
| RAG timeout、依赖不可用、`zero_hit`/`low_confidence` 导致无法证明必需内容 | 保存可信事实、已完成确定性结果和安全错误码；不自动放行。 | `pending_manual/degraded` |
| 结构化输出持续无效 | 保存验证失败类别与确定性检查结果；不保存原始响应。 | `pending_manual/degraded` |
| iteration 2 后仍有任一合规轨失败 | 保存第三轮 revision/review 与安全问题清单。 | `pending_manual/degraded` |
| 商品事实不一致、`products.current_version` 偏离 `base_product_version`、商品或 SKU 不存在、数据库错误、权限变化、租约一致性或 checkpoint 损坏 | 不以模型或降级文本替代事实；版本偏离的安全错误码为 `PRODUCT_VERSION_CONFLICT`。 | `failed` |

可降级不等于可放行：任何 `pending_manual/degraded` 方案都不是 `draft_ready`。错误记录只含稳定代码、节点、iteration、耗时和安全关联 ID，不含敏感内容。

## 10. 安全与审计边界

所有请求和 Worker 操作使用参数化 ORM 查询，服务端注入店铺、商品、SKU 与候选范围。模型不可构造 SQL、文件路径、跨店铺 ID 或未经验证的产品事实。即时数据库授权优先于 JWT claims 和前端显示。

checkpoint、应用日志和 `agent_calls` 不保存 API Key、Cookie、Authorization、完整 Prompt、原始响应或思维链。它们仅保存验证后的业务字段、输入哈希、模型名、Prompt 版本、独立节点名、iteration、attempt、token 统计、耗时、可选成本和安全错误码。审计事件可记录 request ID、actor ID、store/proposal/revision/review/run ID、动作、状态与耗时，但不记录商品全文、查询、路径、向量或凭据。

所有客户端错误使用稳定安全码和短消息；内部堆栈、数据库细节和外部服务响应不返回客户端。只有 Worker 的 DeepSeek client 在真正发起请求的边界读取 `SecretStr`；API 路由和业务逻辑不访问、记录、输出或调用该 Key。

## 11. 测试矩阵

| 层级 | 必须覆盖 |
|---|---|
| 常规单元与 API 测试 | 全部 DeepSeek、RAG、BGE、reranker 与 Milvus 依赖均使用 Mock 或 fake；覆盖输出 Schema、可信事实回读、非法引用、价格/SKU 建议拒绝、双轨放行、3 次上限、错误降级、RBAC 与幂等。 |
| SQLite Worker 测试 | 类型隔离领取、owner guard、续租失败停止、checkpoint 幂等恢复、不可变 revision/review、`agent_calls.iteration` 唯一性与不重复写入。 |
| PostgreSQL opt-in | 显式验证迁移、并发选品唯一性、两 Worker `SKIP LOCKED`、过期租约恢复、旧 owner 拒写、checkpoint 恢复和仅删除精确测试 ID 的清理。 |
| 本地 RAG opt-in | 仅复用 D 盘离线 BGE-M3、reranker 和 Milvus；不下载模型、不访问模型仓库，验证 active citation 与故障降级。 |
| 真实双 Agent 契约烟测 | 最后、显式授权后运行；每个 Agent 最多一次请求，不做传输重试或自动修订，只验证已解析结构与安全元数据，绝不打印或保存原始响应。 |
| 完整门禁 | 全量 `pytest`、`compileall`、Alembic `current` 与 `check`、`docker compose config`、`git diff --check` 和 Git 状态检查。 |

普通测试不得联网、不得访问真实 DeepSeek、不得加载本地生成模型，也不得启动真实平台行为。真实 PostgreSQL、Milvus、D 盘本地 RAG 和真实双 Agent 烟测都必须由各自显式 opt-in 标记与人工授权控制。

## 12. 验收标准

1. `workflow_runs` 能在不破坏现有分析记录的前提下区分 `analysis` 与 `optimization`；日期约束、Worker 领取和状态组合均按类型执行。
2. 已授权店铺 operator 的首次选品在一笔事务中完成分析终态和优化运行创建，返回 `202`；同候选重放返回 `200`，改选稳定返回 `409/ANALYSIS_SELECTION_CONFLICT`，并发不会创建重复资源。
3. 每份方案最多有 3 个不可变 revision 与 3 个对应审核，双轨审核都通过才可见为 `draft_ready`。
4. 标题、卖点、详情、关键词、属性补全、changes 和引用均可追溯；所有价格与 SKU 输出仅是带当前值、建议值、理由和证据的建议，不应用至商品事实。
5. 伪造当前值、未知 SKU、无来源属性、非法引用、非正价格和超过 ±30% 的价格建议均被服务器拒绝。
6. DeepSeek、RAG 或结构化输出持续失败得到可审计的 `pending_manual/degraded`，没有自动放行或编造文案；事实层、数据库、权限与 checkpoint 错误得到 `failed`。
7. 每个外部调用前成功续租；失租 Worker 无法产生任何后续外部调用或持久化写入；恢复不重复 proposal revision、review 或 agent call。
8. 真实、范围内的 operator/supervisor/admin 只能读取当前店铺范围方案，且 API 进程不调用 DeepSeek。
9. 测试矩阵中的 Mock、opt-in PostgreSQL、本地 RAG 与最终真实双 Agent 门禁均有对应证据，且敏感数据不进入持久化、日志或客户端响应。

## 13. 与既有阶段的兼容性与未实现边界

本设计扩展持久经营分析阶段的 `workflow_runs` 和 `agent_calls`，但保持分析工具、分析图、分析 Worker、既有候选事实和 `awaiting_selection` 之前的语义不变。选品只消费已持久化的可信 `analysis_candidates`，并以事务把分析终点推进为 `completed/product_selected`。

本设计复用知识检索阶段的 PostgreSQL canonical source、active-version 过滤、固定本地 BGE-M3/reranker、Milvus stable chunk ID、校准配置、引用和安全错误边界；它不更改知识文档、索引、校准文件或检索 API 契约。

阶段完成仍不包含人工编辑方案、主管审批、模拟发布、回写商品、回写 SKU 或价格、前端、真实电商平台、云服务、生产 SLA、跨公司租户、队列系统或任何本地生成式大语言模型。对外表述只能说明已实现并验证的本地演示能力，不能把此设计本身表述为已上线或已实现能力。
