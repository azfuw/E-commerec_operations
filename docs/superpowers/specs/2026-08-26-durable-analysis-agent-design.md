# Durable Analysis Agent Slice Design

> 状态：已批准的阶段二设计，尚未实现<br>
> 日期：2026-08-26<br>
> 项目路径：`D:\E-commerce_operations`<br>
> 关联：[总设计](2026-08-25-ecommerce-operations-design.md) 第 4、5.1、6.1、7、10、11、13、15 节

## 1. 目标与结束边界

本阶段交付一个完整、可恢复的经营分析纵向切片：已授权用户创建 `POST /analysis-runs` 后，系统把 `workflow_runs` 置为 `accepted`；同仓库的独立 Worker 从 PostgreSQL 领取租约，使用 `workflow_run.id` 作为 LangGraph `thread_id` 执行分析图，调用现有五个只读确定性工具，再由 DeepSeek 经营分析 Agent 解释并排序。服务端保存可信候选与调用审计，工作流最终停在 `awaiting_selection`。

FastAPI 与 Worker 是同一仓库的两个进程。PostgreSQL 是任务、租约、检查点引用、候选和调用结果的事实源；LangGraph PostgreSQL Checkpointer 保存节点间状态，使被租约重领的工作流从已持久化节点恢复，而不是重新猜测或重复写入。

本阶段不实现人工选品接口。`awaiting_selection` 是明确终点；下一阶段才设计选品动作和商品优化。

## 2. 非目标与范围护栏

仅新增应用领域表 `workflow_runs`、`analysis_candidates` 与 `agent_calls`。LangGraph Checkpointer 所需表由其 PostgreSQL 集成管理，不手写为额外业务表。

本阶段不新增优化、审批、知识库或 RAG 表，不实现商品选择、优化、合规、审批或发布。也不引入 Redis、Celery、Kafka、MCP、微服务、前端、Milvus、Docker API、独立 Worker 服务或真实电商平台集成。Worker 是仓库内可独立启动的进程，不是 Compose 新服务。

DeepSeek 是唯一 LLM 调用对象，且只用于经营解释。所有业务事实、候选商品范围、商品编码、指标、异常类型、数值业务影响和证据均由服务端现有确定性工具产生；模型不能提供或覆盖这些事实。

## 3. 架构与进程边界

```text
授权用户
  │ POST /analysis-runs
  ▼
FastAPI ──事务写入──► PostgreSQL workflow_runs(status=accepted)
                              │
                              │ FOR UPDATE SKIP LOCKED + PostgreSQL now()
                              ▼
                    独立 Worker（同一仓库、第二进程）
                              │
                              ▼
          LangGraph + PostgreSQL Checkpointer
          thread_id = workflow_run.id
                              │
       现有五个只读确定性分析工具
                              │  可信候选与指标
                              ▼
                httpx 直连 DeepSeek
                              │  仅解释/建议/置信度
                              ▼
       服务端验证、可信事实重附加、幂等写入
                              │
                              ▼
       analysis_candidates + agent_calls + awaiting_selection
```

API 只创建和读取事实；不在请求处理器内执行 LLM。Worker 只领取可处理工作流，不能绕过 API 的已保存 `store_id`、日期范围或创建者信息。所有数据库查询都使用运行时重新加载的当前用户、店铺和资源归属，而不把 JWT 内的角色或店铺范围当作最终授权依据。

## 4. 数据模型与约束

### 4.1 `workflow_runs`

| 字段 | 约束与含义 |
|---|---|
| `id` | UUID 主键；也是 LangGraph `thread_id`。 |
| `workflow_type` | 固定为 `analysis`。 |
| `store_id`、`created_by` | 分别外键到店铺和用户；所有 API 读取都以 `store_id` 进行实时授权。 |
| `start_date`、`end_date` | 自然日闭区间；数据库约束 `start_date <= end_date`，服务端限定 1–90 天。 |
| `status` | 仅 `accepted`、`processing`、`awaiting_selection`、`failed`。 |
| `quality_status` | 枚举支持 `normal`、`partial`、`degraded`；本阶段写入仅为 `normal` 或 `degraded`。 |
| `attempt_count` | 非负且不超过 3；每次成功领取时原子加一。 |
| `lease_owner`、`lease_expires_at` | 可空；只表示当前 `processing` 租约。所有时间比较使用 PostgreSQL `now()`。 |
| `current_step` | 已持久化图节点名，供恢复与安全读取。 |
| `input`、`output`、`quality` | JSON 业务数据；输入保存店铺与日期，输出仅保存安全的候选摘要和质量信息。 |
| `error_code` | 可空稳定安全错误码；不保存堆栈、密钥或完整模型内容。 |
| `created_at`、`updated_at` | 数据库时间戳。 |

`status`、`quality_status`、日期和尝试次数均有 `CHECK` 约束。终态 `awaiting_selection` 与 `failed` 不允许再次被领取。终态更新必须带当前 `lease_owner` 条件，避免过期 Worker 覆盖后来租约持有者的结果。

### 4.2 `analysis_candidates`

每行是一个可供后续人工选择的可信候选，包含：`workflow_run_id`、`product_id`、`rank`、服务端重附加的 `product_code`、`anomaly_types`、`metrics`、数值 `business_impact`、`evidence`，以及模型提供的 `impact_explanation`、`reason`、`recommended_action`、`confidence`。

`(workflow_run_id, product_id)` 与 `(workflow_run_id, rank)` 均唯一；`rank >= 1`，`confidence` 在闭区间 `[0, 1]`。候选写入以这两个唯一约束进行幂等 upsert：同一工作流重放时替换相同可信事实，而不新增候选。候选数量由服务端确定性结果决定；种子验收数据中必须为 5 个可信候选。

### 4.3 `agent_calls`

每个外部调用或降级决定都有一行，关联 `workflow_run_id`，并记录 `node_name`、逻辑调用类型（`primary` 或 `schema_repair`）、尝试序号、模型、Prompt 版本、状态、输入哈希、tokens、耗时、可选 `estimated_cost` 和安全错误码。`(workflow_run_id, node_name, call_type, attempt)` 唯一，保证租约恢复不会重复计入相同调用。

`input_hash` 是规范化、非秘密事实输入的 SHA-256 哈希。表绝不保存 API Key、Authorization、完整 Prompt、完整响应、思维链或未脱敏异常。模型价格没有配置时 `estimated_cost` 为 `null`，不猜测成本。

## 5. 状态机、领取与恢复

### 5.1 状态机

```text
accepted ──领取──► processing ──正常或降级完成──► awaiting_selection
                         │
                         └──事实层、输入或检查点错误──► failed
```

`quality_status=normal` 对应成功的结构化模型结果；`quality_status=degraded` 对应可用的确定性中文降级候选。`partial` 保留在模式中供总架构后续阶段使用，但本阶段不写入该值。网络、限流、模型认证和模型结构问题不是 `failed`：只要确定性事实层成功，就通过降级结果进入 `awaiting_selection`。

### 5.2 租约领取算法

Worker 公开可测试的 `run_once()`：每次最多领取并处理一个工作流。领取事务使用 PostgreSQL 服务器时间：

1. 先把租约已过期且 `attempt_count >= 3` 的 `processing` 运行标记为 `failed`，错误码为 `LEASE_ATTEMPTS_EXHAUSTED`。
2. 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 选择一个 `accepted`，或一个租约已过期且 `attempt_count < 3` 的 `processing` 运行。
3. 在同一事务内设置新的 `lease_owner`、`lease_expires_at = now() + lease_duration`、`status=processing`，并将 `attempt_count` 加一。
4. 提交后才执行 LangGraph。执行前以及每个会阻塞的外部调用前续租；续租和终态写入都要求当前 `lease_owner` 仍匹配。

两个 Worker 无法同时领取同一行；`SKIP LOCKED` 使后到 Worker 寻找另一行而非阻塞。Worker 崩溃后，租约到期可被新的 `run_once()` 重领；尝试次数耗尽后不重领。`awaiting_selection` 与 `failed` 永远不在领取查询中。

### 5.3 Checkpoint 恢复与写入幂等

图调用配置固定 `thread_id=workflow_run.id`。每个节点完成后由 PostgreSQL Checkpointer 保存状态；被重领时从最后成功 checkpoint 继续。候选和调用记录使用上述唯一约束写入，且终态更新带租约所有者条件，因此节点重放不会制造额外候选、额外调用记录或状态回退。

检查点读取、保存或一致性错误属于事实层错误：记录安全错误码并将工作流置为 `failed`。这与可降级的 LLM 错误严格区分。

## 6. LangGraph 节点与持久状态

图状态只携带 `workflow_run_id`、可信店铺与日期、确定性事实、候选集合、模型草稿、质量状态和安全错误码。它不携带密钥、Authorization 或完整模型原文。

| 节点 | 输入与行为 | 持久结果 |
|---|---|---|
| `load_run` | 读取已租约绑定的运行，核对 `workflow_type`、店铺、日期和状态。 | `current_step=load_run`。 |
| `collect_facts` | 使用服务器注入的 `store_id` 调用全部五个现有只读工具：`get_store_summary`、`find_anomalous_products`、`get_product_metrics`、`compare_store_products`、`get_inventory_risk`。 | 排序后的可信候选、指标、异常、影响和 evidence。 |
| `call_analysis_agent` | 把最小必要的可信事实传给 DeepSeek，并执行 transport 重试策略。 | 只保留结构化草稿或安全调用错误。 |
| `validate_and_reconcile` | 验证模型草稿，必要时做一次结构修复调用；将通过的解释与服务端事实合并。 | 可信候选的完整可展示数据，或降级候选。 |
| `persist_results` | 幂等 upsert 候选和 `agent_calls`，写入质量与终态。 | `status=awaiting_selection`、`current_step=persist_results`。 |

如果确定性候选数为 `N`，模型输出必须恰好覆盖该 `N` 个可信 `product_id`，每个一次，排名为连续的 `1..N`。在验收种子场景，`N=5`。这使未知商品、重复商品、遗漏商品、重复排名和非连续排名都成为可验证的结构错误。

## 7. API 契约与实时授权

### `POST /analysis-runs`

请求体为 `store_id`、`start_date`、`end_date`。请求必须有有效 JWT；服务端重新从数据库加载活跃用户及其当前角色和店铺范围，再确认用户可访问该店铺。日期按自然日闭区间解释，且 `1 <= (end_date - start_date + 1) <= 90`。无权限为 403，无效日期为 422。

通过校验后，API 在一个事务内创建 `workflow_runs(status=accepted, quality_status=normal, attempt_count=0)`，返回 HTTP 202 与 `workflow_run_id`、`status=accepted`。API 不调用 LLM，不获得或输出 API Key。

### `GET /workflow-runs/{id}`

读取前重新加载当前用户并按运行记录的 `store_id` 验证实时店铺权限。成功返回安全的状态、质量、步骤、日期、尝试次数、候选就绪状态和安全错误码；不返回租约所有者、检查点内容、密钥或模型原文。

### `GET /analysis-runs/{id}/candidates`

使用同样的实时数据库店铺范围授权。仅当工作流处于 `awaiting_selection` 时返回排序候选。`accepted`、`processing` 或 `failed` 均返回稳定 HTTP 409，错误码 `ANALYSIS_NOT_READY`，并且不泄露候选内容。此阶段没有 `POST /analysis-runs/{id}/select-product`。

## 8. LLM 结构化输出与可信事实重附加

DeepSeek 通过 `httpx` 直接调用兼容 Chat Completions 接口；不使用 SDK，也不建立供应商工厂。默认模型精确为 `deepseek-v4-flash`，可由 `DEEPSEEK_MODEL` 覆盖。API 进程在没有 API Key 时仍能启动；只有 Worker 的 `call_analysis_agent` 节点检查 Key 是否可用。

模型输出是禁止额外字段的结构化 JSON。每个候选只允许以下字段：

```text
product_id: string
rank: integer
impact_explanation: string
reason: string
recommended_action: string
confidence: number in [0, 1]
```

服务端验证 `product_id` 与可信候选集合的精确覆盖，验证唯一且连续的 `rank`，并验证置信度闭区间。未知或重复商品、遗漏商品、重复或非连续排名、越界置信度，以及 JSON/Pydantic Schema 失败，都被拒绝为结构错误。

通过验证后，服务端按 `product_id` 重附加 `product_code`、指标、异常类型、数值业务影响和 evidence；模型值不能替换它们。服务端仅保留模型的影响解释、原因、建议和置信度。该边界让模型负责语言与建议，而不是业务事实。

## 9. 错误、重试与降级

| 类别 | 行为 | 最终状态与质量 |
|---|---|---|
| 超时、网络错误、HTTP 429、HTTP 5xx | 每个逻辑 LLM 请求最多 3 次 HTTP 尝试，即初次加 2 次重试。 | 仍失败时使用中文降级模板，`awaiting_selection/degraded`。 |
| JSON 或 Schema 结构错误 | 在收到但未通过服务端验证的响应后，最多发起 1 次 `schema_repair` 逻辑调用；该调用不再递归修复。 | 仍不合格时使用中文降级模板，`awaiting_selection/degraded`。 |
| 缺少 API Key、HTTP 401、HTTP 403 | 不重试。 | 使用中文降级模板，`awaiting_selection/degraded`。 |
| 确定性工具、输入验证、数据库事实、租约一致性或 Checkpointer 错误 | 不尝试用 LLM 掩盖。 | `failed`，记录安全错误码。 |

`schema_repair` 也是一个逻辑请求，因此可使用同一 transport 重试上限；它最多发生一次且永不触发第二次修复。HTTP 401/403 不属于可重试 transport 类别。

降级模板根据可信确定性候选生成中文结果：保留产品、排序、指标、异常、数值影响和 evidence，并用固定中文说明模型解释暂不可用、建议人工核验。它不生成新商品事实，也不把失败误报为正常质量。

## 10. 安全与可观测性

JWT 与店铺范围只在服务端验证；每次读取运行或候选都重新查询数据库中的当前用户、角色、状态和店铺范围。SQL 使用参数化 ORM 查询，模型不能提交 SQL、任意店铺 ID 或任意产品事实。

`agent_calls`、应用日志和 API 响应不记录 API Key、Authorization、密码、完整 Token、完整 Prompt、完整模型响应或思维链。错误对客户端以稳定安全码呈现，详细诊断仅通过受控本地日志处理，不写入业务事实表。

可观测记录包括模型名、Prompt 版本、节点名、调用状态、transport 尝试、tokens、耗时、可选成本、输入哈希和安全错误码。Worker 还记录领取、续租、恢复、终态转换和质量状态，使一次分析可以从数据库事实追溯到候选，而无需保存敏感文本。

## 11. 测试矩阵

除最后一条明确的真实服务烟测外，所有测试都使用 Mock 的 `httpx` 行为，不访问外网，也不依赖真实 API Key。

| 层级 | 覆盖 |
|---|---|
| 单元 | 请求日期 1–90 天闭区间、结构化 Schema、未知/重复商品、遗漏/重复/非连续排名、越界置信度、事实重附加、中文降级模板和安全审计字段。 |
| API 集成 | JWT、禁用或未知用户、实时店铺范围、创建 202、跨店读取拒绝、未就绪候选稳定 409。 |
| PostgreSQL Worker | 双 Worker `SKIP LOCKED` 互斥、租约过期恢复、三次领取上限、终态不重领、可测试 `run_once()`。 |
| LangGraph 持久化 | 节点后 checkpoint、重启从 checkpoint 恢复、候选与 `agent_calls` 幂等、过期 Worker 不能覆盖新租约结果。 |
| LLM 失败策略 | 超时、网络、429、5xx 的两次重试；一次 Schema 修复；缺 Key、401、403 和修复失败均降级；事实层、输入和 checkpoint 错误均失败。 |
| PostgreSQL 纵向闭环 | 固定种子数据得到 5 个可信候选，Worker 完成到 `awaiting_selection`，候选包含服务端事实，`agent_calls` 可审计。 |
| 明确真实烟测 | 在所有 Mock 测试之后，显式授权的最小 `deepseek-v4-flash` 请求验证真实配置与结构化响应；只记录安全元数据，不打印或保存 Key、Authorization、完整 Prompt 或完整响应。 |

## 12. 验收标准

1. 已授权用户能以 1–90 天闭区间创建 `accepted` 分析运行，未授权店铺不能创建或读取。
2. 两个 Worker 竞争同一 PostgreSQL 队列时只处理不同工作流；租约过期可恢复，第三次领取后不再重领。
3. LangGraph 使用 `workflow_run.id` 作为 `thread_id`，可从 PostgreSQL checkpoint 恢复。
4. 正常路径调用全部五个确定性工具，在种子数据上保存 5 个可信候选，并以服务端重附加事实。
5. 候选和 `agent_calls` 在恢复或重放后保持幂等，终态不会回退。
6. 超时、网络、429、5xx、结构错误、缺 Key、401、403 均产生确定性降级候选并到达 `awaiting_selection/degraded`；事实层、输入和 checkpoint 错误到达 `failed`。
7. `agent_calls` 不含密钥、Authorization、完整 Prompt/响应或思维链；价格未配置时成本为 `null`。
8. 常规测试全部 Mock，最后仅运行一次显式授权的最小 `deepseek-v4-flash` 真实烟测。
9. 阶段终点固定为 `awaiting_selection`，不包含人工选品或商品优化实现。

## 13. 与总设计的一致性及未实现范围

本设计把总设计的 PostgreSQL 租约、LangGraph checkpoint、DeepSeek 结构化调用、确定性分析工具、RBAC 与审计边界收敛为最小可验证切片。总设计中仍属于后续范围的人工选品、商品优化、合规审核、审批、模拟发布、RAG、Milvus、其他 Worker 服务、前端、平台集成、知识库与评测管理均未在本阶段实现或声明为已实现。

本阶段完成时仅证明经营分析从 `accepted` 到 `awaiting_selection` 的可恢复闭环。任何后续设计或实现都必须保持服务端事实重附加、数据库实时店铺范围授权、可恢复租约和敏感 LLM 数据最小化记录这些边界。
