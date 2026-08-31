# 人工修订、主管审批与本地模拟发布阶段设计

> 状态：已批准的阶段五设计，尚未实施
> 日期：2026-08-31
> 项目路径：`D:\E-commerce_operations`
> 上游：[总设计](2026-08-25-ecommerce-operations-design.md)、[持久经营分析 Agent 设计](2026-08-26-durable-analysis-agent-design.md)、[商品优化与合规设计](2026-08-28-product-optimization-compliance-design.md)

## 1. Goal / Non-goals

### 1.1 阶段目标

本阶段把当前止于 `draft_ready` 或 `pending_manual` 的商品方案补成一个本地、可恢复、可审计的业务闭环：

```text
人工修订
→ 独立 manual_review Workflow + Worker
→ 确定性检查 + 独立合规 Agent
→ draft_ready
→ 提交主管审批
→ approve | reject | request_changes
→ 幂等本地模拟发布或回到人工处理
```

交付范围包括人工修订、独立人工复核工作流与 CLI、主管审批、幂等本地模拟发布和只追加审计。前端不在本阶段，所有验收通过 API、Worker、数据库事实和测试完成。

模拟发布只更新本地 PostgreSQL `products` 当前 Listing 的以下字段：

- `title`
- `selling_points`
- `description`
- `search_keywords`
- 经过可信事实和引用验证的 `attribute_completions` 对应 `attributes`
- `current_version = current_version + 1`

发布绝不更新 `product_skus.code`、`product_skus.spec`、价格、当前库存、库存快照、订单或流量，也不调用淘宝、京东或任何真实电商平台。

### 1.2 非目标

- 不新增价格专员角色、价格审批权限或价格/SKU 修改流程。
- `price_suggestions` 与 `sku_suggestions` 继续只读保留，不能由人工修订请求改写，也不会在发布时应用。
- 不修改现有商品优化 Agent、合规 Agent Prompt、DeepSeek JSON runtime 或 provider client。
- 不改变原优化 Worker 的自动 iteration `0..2`、最多三轮、第四轮禁止和 `pending_manual` 降级语义。
- 不把 `iteration` 改造成通用方案版本号；人工与自动版本统一使用新增 `revision_number` 排序。
- 不实现前端、真实平台发布、真实价格权限、多商户 SaaS、微服务、队列系统或新的模型供应商。
- 不新增真实 DeepSeek smoke；新编排只用 `httpx.MockTransport` 验证现有合规 client。

## 2. Approved decisions

1. 人工可编辑 `title`、`selling_points`、结构化 `description`、`keywords`、`attribute_completions`，并提交与这些字段一致的结构化 `changes` 和 `evidence`。
2. 客户端不得提交 `citations`、`price_suggestions` 或 `sku_suggestions`。服务端只从当前父 revision 安全继承这三个只读字段，并在写入前重新验证商品事实、SKU 当前值和 canonical citation 的 active/current/applicable 归属。
3. 人工 revision 写入后必须重新经过确定性规则和独立合规 Agent；两轨在同一 revision 都通过才回到 `draft_ready`。人工流程绝不调用商品优化 Agent。
4. 每次人工修订创建全新的 `WorkflowRun(workflow_type=manual_review)`、workflow ID、LangGraph `thread_id` 和 checkpoint 链；不重开原 optimization run。
5. 同一 proposal 同时最多一个活动人工复核。创建和清理 `active_manual_review_run_id` 都在锁定 proposal 的事务内完成。
6. `operator` 可人工修订和提交，不能审批；`supervisor` 与 `admin` 可人工修订、提交、批准、驳回和要求修改，并允许批准自己提交的方案。
7. `admin` 不绕过店铺授权。所有角色的每次请求和 Worker 事实加载都重新读取 active user、当前角色、精确 `UserStoreScope`、店铺状态和资源归属。
8. `draft_ready` 与 `pending_manual` 可人工修订；`pending_approval`、`completed`、`rejected`、`failed` 禁止编辑。
9. 主管批准是一个锁定事务，商品更新、版本递增、批准动作、发布记录和审计要么全部提交，要么全部回滚。
10. 重复批准返回同一个 `publish_record`，不能再次递增商品版本；并发冲突、冲突审批动作和旧商品版本使用稳定 HTTP 409。

## 3. Architecture and data flow

```text
FastAPI
├─ POST /proposals/{id}/manual-revision
│    └─ 锁 proposal/original run/product/current revision
│       → 组合 client editable + server inherited readonly
│       → 新 ProposalRevision(origin=manual, iteration=NULL)
│       → 新 manual_review WorkflowRun + ManualReviewRun
│       → original optimization run = pending_manual/manual_review_pending
├─ POST /proposals/{id}/submit
│    └─ 锁定并记录当前 passed revision → pending_approval
├─ GET /approvals
├─ POST /approvals/{proposal_id}/approve|reject|request-changes
├─ GET /audit-events
└─ GET /proposals/{id} 扩展安全视图

Manual Review Worker（独立进程与 CLI）
└─ 只领取 workflow_type=manual_review
   → fresh auth/context/product/revision
   → trusted-input loader + active citation recheck
   → deterministic validation
   → independent compliance client（不调用 optimization Agent）
   → immutable ComplianceReview
   → original optimization run = draft_ready | pending_manual | failed

Supervisor/Admin approve
└─ 单一 PostgreSQL 锁定事务
   → revalidate submitted revision/review/product version/citations
   → 更新允许的 Product Listing 字段并 version + 1
   → ApprovalAction + PublishRecord + AuditEvent
   → original optimization run = completed/simulated_published
```

API 进程不调用 DeepSeek。Manual Review Worker 是唯一可能在本阶段调用 DeepSeek 的新编排入口，且只调用现有 `ProductComplianceAgentClient`。

## 4. State machines

### 4.1 原 optimization run 作为 proposal 生命周期事实源

`ProductProposal` 继续不复制 lifecycle status；关联的原 `optimization_run_id` 是 proposal 状态事实源。新增状态转换如下：

```text
自动优化：accepted → processing → draft_ready | pending_manual | failed

draft_ready ──manual revision──► pending_manual/manual_review_pending
pending_manual ──manual revision──► pending_manual/manual_review_pending

manual review passed ──────────► draft_ready/manual_review_passed
manual review valid non-pass ──► pending_manual/manual_review_changes_required
manual review dependency degrade► pending_manual/manual_review_degraded
manual review fatal fact error ─► failed/manual_review_failed

draft_ready ──submit────────────► pending_approval
pending_approval ──approve──────► completed/simulated_published
pending_approval ──reject───────► rejected
pending_approval ──request_changes► pending_manual/approval_changes_requested
```

状态和质量组合固定为：

| 状态/步骤 | quality | error_code |
|---|---|---|
| `pending_manual/manual_review_pending` | `normal` | `NULL` |
| `draft_ready/manual_review_passed` | `normal` | `NULL` |
| `pending_manual/manual_review_changes_required` | `normal` | `NULL` |
| `pending_manual/manual_review_degraded` | `degraded` | 对应安全依赖码 |
| `pending_approval` | `normal` | `NULL` |
| `completed/simulated_published` | `normal` | `NULL` |
| `rejected` | `normal` | `NULL` |
| fatal `failed/manual_review_failed` | `degraded` | 对应安全事实码 |

### 4.2 manual_review workflow

```text
accepted ──claim──► processing ──valid result──► completed
                         ├─ dependency degraded result ─► completed/degraded
                         └─ fact/database/checkpoint error ─► failed
```

- `completed/normal` 可表示两轨通过，也可表示业务上有效的非通过审核；是否通过只读关联 `ComplianceReview.passed`。
- `completed/degraded` 表示已安全持久化依赖失败 review，原 proposal 保持 `pending_manual`。
- `failed` 只用于商品版本、授权、事实、数据库、checkpoint 或 replay 一致性错误；对应原 optimization run 原子进入 `failed`。
- terminal manual run 不再领取；原 owner 失租或收到 `CancelledError` 后不写任何 revision、review、AgentCall、指针或终态。

## 5. Schema, migration and constraints

### 5.1 `workflow_runs` 兼容扩展

新增 `WorkflowType.MANUAL_REVIEW = "manual_review"`，新增 `WorkflowStatus.PENDING_APPROVAL = "pending_approval"` 与 `WorkflowStatus.REJECTED = "rejected"`。替换 `ck_workflow_runs_type_status_dates`，精确允许：

| workflow_type | 日期 | status |
|---|---|---|
| `analysis` | `start_date/end_date` 非空且有序 | `accepted, processing, awaiting_selection, completed, failed` |
| `optimization` | 两日期均 `NULL` | `accepted, processing, draft_ready, pending_manual, pending_approval, completed, rejected, failed` |
| `manual_review` | 两日期均 `NULL` | `accepted, processing, completed, failed` |

现有 lease-state、quality 和 attempt `0..3` 约束保持。新增领取索引 `ix_workflow_runs_type_status_lease_created`：`(workflow_type, status, lease_expires_at, created_at, id)`，供各类型 `SKIP LOCKED` 查询使用；领取查询仍必须显式限定 workflow type。

### 5.2 `proposal_revisions` 无损迁移

新增列：

| 列 | 类型/限制 | 含义 |
|---|---|---|
| `revision_number` | `Integer NOT NULL`, `>= 1` | proposal 内连续业务版本号。 |
| `origin` | `String(16) NOT NULL` | 仅 `agent` 或 `manual`。 |
| `created_by` | `String(36) NOT NULL FK users.id` | 创建该不可变版本的用户。 |
| `parent_revision_id` | `String(36) NULL FK proposal_revisions.id` | 同 proposal 的直接父版本。 |

迁移顺序固定为：

1. 四列先以 nullable 形式加入。
2. 对所有既有行设置 `revision_number = iteration + 1`、`origin = 'agent'`。
3. `created_by` 从该 proposal 的 `optimization_run_id → workflow_runs.created_by` 回填。
4. iteration `1`、`2` 的既有行以同 proposal、`iteration - 1` 的 revision 回填 `parent_revision_id`；iteration `0` 保持 `NULL`。缺行、重复或归属不一致使迁移失败，不猜测父版本。
5. 验证回填无空值、`(proposal_id, revision_number)` 无重复后，把 `revision_number`、`origin`、`created_by` 改为非空。
6. `iteration` 改为 nullable；保留既有 `(proposal_id, iteration)` 唯一约束，以 PostgreSQL 的 nullable unique 语义继续保护 agent iteration。
7. 新增 `uq_proposal_revisions_proposal_revision_number(proposal_id, revision_number)`、上述 FK，以及以下 CHECK：
   - `ck_proposal_revisions_revision_number`: `revision_number >= 1`
   - `ck_proposal_revisions_origin`: `origin IN ('agent', 'manual')`
   - `ck_proposal_revisions_origin_iteration`: `(origin='agent' AND iteration BETWEEN 0 AND 2) OR (origin='manual' AND iteration IS NULL)`
   - `ck_proposal_revisions_parent`: `(revision_number=1 AND parent_revision_id IS NULL) OR (revision_number>1 AND parent_revision_id IS NOT NULL)`
   - `ck_proposal_revisions_not_self_parent`: `parent_revision_id IS NULL OR parent_revision_id <> id`

服务层在锁定 `product_proposals` 后以当前最大 `revision_number + 1` 创建下一版本，并验证 parent 属于同 proposal 且编号正好少一。不可变 revision 不提供 update/delete 业务路径，所以成功事务不会产生已提交的编号空洞。

自动版本保持 `iteration=0..2`；人工版本固定 `iteration=NULL`。`revision_number` 是显示和父链编号，绝不反向改变自动 iteration 的含义。

### 5.3 `compliance_reviews`

`iteration` 改为 nullable，将 CHECK 替换为 `iteration IS NULL OR iteration BETWEEN 0 AND 2`。既有值不变：agent revision 仍为 `0..2`，manual revision 的 review 固定为 `NULL`。`proposal_revision_id` 唯一约束继续保证每个 revision 最多一个 immutable review；现有 `(proposal_id, iteration)` unique 继续保护 agent review，nullable manual rows由 `proposal_revision_id` 唯一性保护。服务层验证 review 的 iteration 与 revision origin 精确匹配。

### 5.4 `product_proposals` 新指针

新增：

- `active_manual_review_run_id String(36) NULL FK manual_review_runs.id`
- `submitted_revision_id String(36) NULL FK proposal_revisions.id`
- `uq_product_proposals_active_manual_review_run_id`（nullable unique）
- `ix_product_proposals_submitted_revision_id(submitted_revision_id)`

`active_manual_review_run_id` 只指向属于同 proposal、非 terminal 的 manual run。创建、terminal 清理和失租重放均锁 proposal；不能以普通 update 绕过。`submitted_revision_id` 只在 `pending_approval` 或已发布/驳回的历史视图中保留精确审批版本；`request_changes` 原子清空它。

### 5.5 新 `manual_review_runs`

| 列 | 类型/限制 |
|---|---|
| `id` | `String(36) PK` |
| `workflow_run_id` | `String(36) NOT NULL UNIQUE FK workflow_runs.id` |
| `proposal_id` | `String(36) NOT NULL FK product_proposals.id` |
| `proposal_revision_id` | `String(36) NOT NULL UNIQUE FK proposal_revisions.id` |
| `submitted_by` | `String(36) NOT NULL FK users.id` |
| `idempotency_key_hash` | `String(64) NOT NULL`, length exactly 64 |
| `request_hash` | `String(64) NOT NULL`, length exactly 64 |
| `created_at`, `updated_at` | timezone-aware、非空数据库时间 |

唯一约束 `uq_manual_review_runs_proposal_actor_key(proposal_id, submitted_by, idempotency_key_hash)` 支持精确重放；索引 `ix_manual_review_runs_proposal_created(proposal_id, created_at, id)` 支持历史读取。workflow run、revision、proposal、submitter、store 和 product 的归属链由服务层在每次写入及恢复时重新验证。

哈希 CHECK 精确为 `ck_manual_review_runs_idempotency_hash_length: length(idempotency_key_hash)=64` 与 `ck_manual_review_runs_request_hash_length: length(request_hash)=64`。

### 5.6 新 `approval_actions`（append-only）

| 列 | 类型/限制 |
|---|---|
| `id` | `String(36) PK` |
| `proposal_id` | `String(36) NOT NULL FK product_proposals.id` |
| `proposal_revision_id` | `String(36) NOT NULL FK proposal_revisions.id` |
| `store_id` | `String(36) NOT NULL FK stores.id` |
| `actor_id` | `String(36) NOT NULL FK users.id` |
| `actor_role` | `String(16) NOT NULL`, `operator|supervisor|admin` |
| `action` | `String(32) NOT NULL`, `submit|approve|reject|request_changes` |
| `comment` | `String(500) NULL` |
| `idempotency_key_hash` | `String(64) NOT NULL`, length 64 |
| `request_hash` | `String(64) NOT NULL`, length 64 |
| `created_at` | timezone-aware、非空数据库时间 |

约束：

- `uq_approval_actions_proposal_actor_action_key(proposal_id, actor_id, action, idempotency_key_hash)`
- `ck_approval_actions_action`: `action IN ('submit','approve','reject','request_changes')`
- `ck_approval_actions_actor_role`: `actor_role IN ('operator','supervisor','admin')`
- `ck_approval_actions_idempotency_hash_length`: `length(idempotency_key_hash)=64`
- `ck_approval_actions_request_hash_length`: `length(request_hash)=64`
- `ck_approval_actions_comment`: `reject/request_changes` 必须有 trim 后长度 `1..500` 的 comment；`submit/approve` 的 comment 必须为 `NULL`
- `ix_approval_actions_proposal_created(proposal_id, created_at, id)`
- `ix_approval_actions_store_created(store_id, created_at, id)`

业务层没有更新或删除 approval action 的入口。重放读取既有 action，不追加第二行。

### 5.7 新 `publish_records`（append-only）

| 列 | 类型/限制 |
|---|---|
| `id` | `String(36) PK` |
| `proposal_id` | `String(36) NOT NULL UNIQUE FK product_proposals.id` |
| `proposal_revision_id` | `String(36) NOT NULL UNIQUE FK proposal_revisions.id` |
| `product_id` | `String(36) NOT NULL FK products.id` |
| `store_id` | `String(36) NOT NULL FK stores.id` |
| `approved_by` | `String(36) NOT NULL FK users.id` |
| `approval_action_id` | `String(36) NOT NULL UNIQUE FK approval_actions.id` |
| `publish_idempotency_hash` | `String(64) NOT NULL UNIQUE`, length 64 |
| `before_snapshot`, `after_snapshot` | `JSON NOT NULL` |
| `base_product_version` | `Integer NOT NULL`, `>=1` |
| `published_product_version` | `Integer NOT NULL` |
| `published_at` | timezone-aware、非空数据库时间 |

CHECK 精确为 `ck_publish_records_base_version: base_product_version>=1`、`ck_publish_records_version_increment: published_product_version=base_product_version+1` 与 `ck_publish_records_idempotency_hash_length: length(publish_idempotency_hash)=64`。`publish_idempotency_hash` 是服务端对固定域、proposal ID 和 submitted revision ID 的规范化 SHA-256，不保存请求头原值。快照只含 `title`、`selling_points`、最终 Text `description`、`search_keywords`、`attributes`、`current_version`；绝不含价格、SKU、库存、Key、Prompt 或 provider 数据。索引 `ix_publish_records_store_published(store_id, published_at, id)` 与 `ix_publish_records_product_published(product_id, published_at, id)` 支持审计查询。

### 5.8 新 `audit_events`（append-only）

| 列 | 类型/限制 |
|---|---|
| `id` | `String(36) PK` |
| `event_type` | `String(64) NOT NULL` |
| `outcome` | `String(16) NOT NULL`, `success|failed|denied` |
| `actor_id` | `String(36) NULL FK users.id`，Worker 系统事件可空 |
| `actor_role` | `String(16) NULL` |
| `store_id` | `String(36) NOT NULL FK stores.id` |
| `proposal_id` | `String(36) NULL FK product_proposals.id` |
| `proposal_revision_id` | `String(36) NULL FK proposal_revisions.id` |
| `workflow_run_id` | `String(36) NULL FK workflow_runs.id` |
| `approval_action_id` | `String(36) NULL FK approval_actions.id` |
| `publish_record_id` | `String(36) NULL FK publish_records.id` |
| `request_id` | `String(64) NULL` |
| `error_code` | `String(64) NULL` |
| `details` | `JSON NOT NULL`, 默认 `{}` |
| `created_at` | timezone-aware、非空数据库时间 |

`event_type` 固定闭集：`manual_revision_created`、`manual_review_claimed`、`manual_review_completed`、`manual_review_failed`、`proposal_submitted`、`proposal_approved`、`proposal_rejected`、`proposal_changes_requested`、`simulated_publish_completed`、`authorization_denied`。索引：

- `ck_audit_events_event_type`：`event_type` 只能取上述闭集
- `ck_audit_events_outcome`：`outcome IN ('success','failed','denied')`
- `ck_audit_events_actor_role`：`actor_role IS NULL OR actor_role IN ('operator','supervisor','admin')`

- `ix_audit_events_store_created(store_id, created_at, id)`
- `ix_audit_events_proposal_created(proposal_id, created_at, id)`
- `ix_audit_events_workflow_created(workflow_run_id, created_at, id)`
- `ix_audit_events_actor_created(actor_id, created_at, id)`

业务层不提供 update/delete。`details` 规范化 JSON 上限 4096 bytes，键只允许 `from_status`、`to_status`、`revision_number`、`origin`、`workflow_type`、`quality_status`、`current_step`、`changed_fields`、`review_passed`、`risk_level`、`published_from_version`、`published_to_version`。标识使用专用列，不塞入 details。

### 5.9 迁移安全

迁移先扩展旧表和回填，再创建新表、索引和循环 FK，最后收紧 nullable/CHECK。升级在一个 PostgreSQL DDL 事务中执行；任何回填、归属或唯一性验证失败都整体回滚。降级在存在任何 manual revision、manual_review workflow、manual review run、approval action、publish record 或 audit event 时明确拒绝，避免丢失业务事实。

## 6. Manual input and trusted evidence

### 6.1 请求边界

`POST /proposals/{id}/manual-revision` 请求必须 `extra=forbid`，字段固定为：

- `parent_revision_id: String(36)`
- `base_product_version: Integer >= 1`
- `title: String`，业务长度 `1..60` 且包含中文
- `selling_points: 1..5`，每项 `1..80`
- `description: 1..10 DescriptionSection`，heading `1..40`、body `1..1000`
- `keywords: 1..20`，每项 `1..32`
- `attribute_completions: 0..20`
- `changes: 0..4`，field 仍仅 `title|selling_points|description|keywords`

`DescriptionSection`、`AttributeCompletion`、`OptimizationChange` 与 `EvidenceRef(kind=fact|citation)` 复用现有严格 Schema。请求模型没有 `citations`、`price_suggestions`、`sku_suggestions` 字段，因此客户端添加这些键固定 422。

### 6.2 服务端组合与验证

服务在同一事务中 fresh-load 并锁定 actor、精确 scope、proposal、原 optimization run、Product、全部当前 SKU、当前 parent revision 和 review。随后：

1. 确认状态只可能是 `draft_ready` 或 `pending_manual`，无 active manual run，parent 正是 current revision，商品版本仍等于 proposal base version。
2. 从请求取得五类可编辑输出；从 parent revision 原样读取 `citations`、`price_suggestions`、`sku_suggestions`。
3. 重新从 PostgreSQL 验证 inherited citation 的 document/version/chunk 双向归属、document enabled、version active/current、类别适用和 canonical text 精确一致。
4. 重新验证 inherited price/SKU suggestion 的目标 SKU、current price/code/spec 与 fresh DB 一致；它们仍只读。
5. 组合完整 `OptimizationProposalOutput`，运行现有 typed parser 和 deterministic validator。
6. 信任边界违规（未知/重复引用、无效 fact path、current/suggested mismatch、未声明变更、重复 target、未知 SKU、伪造当前值、无来源新属性）固定拒绝且零 revision/run/pointer/audit 写入。可审核的文案违规由 Worker 的 deterministic track 持久化，不由 API 假装通过。

fact path 和 citation allowlist 与当前 validator 完全一致；属性 key 和 SKU ID 中的点号继续按已验证的固定前缀/末尾字段解析。客户端不能通过删除 citations 绕过证据要求，因为 citations 从 parent 安全继承。

每个成功人工 revision 保存 fresh trusted fact hash、父 revision、创建者、`origin=manual`、`iteration=NULL` 和下一连续 `revision_number`。创建事务同时写 manual workflow/run、`manual_revision_created` audit、current revision pointer、active manual pointer，并把原 optimization run 置为 `pending_manual/manual_review_pending`。任一步失败全部回滚。

## 7. API, RBAC and idempotency

### 7.1 通用实时授权

每个 API 动作都重新查询 `User(status=active)`、当前角色、enabled Store、精确 `(user_id, store_id)` 的 `UserStoreScope`、proposal/revision/run/product 归属。所有角色包括 admin 都必须有该 scope。跨店、未知资源或归属不一致返回不泄露资源的 404；角色不足返回 403。

所有 POST 写接口要求 trim 后长度 `1..128` 的 `Idempotency-Key`。数据库只保存 SHA-256 hash。`request_hash` 来自 action、actor、proposal、目标 revision、规范化请求体和 base version 的稳定 JSON。相同 key + 相同 request hash 返回既有结果；相同 key + 不同 request hash 返回 `409/IDEMPOTENCY_REPLAY_CONFLICT`。日志、响应、audit details 不含原 key 或 hash。

### 7.2 API 契约

| API | 角色 | 关键行为 |
|---|---|---|
| `POST /proposals/{id}/manual-revision` | operator/supervisor/admin | 首次返回 202 与 `revision_id/manual_review_workflow_run_id/status=accepted`；精确重放 200。 |
| `POST /proposals/{id}/submit` | operator/supervisor/admin | 请求含 current `revision_id`；仅 `draft_ready`、normal passed review、无 active manual run可提交。 |
| `GET /approvals` | supervisor/admin | 仅列出调用者 scope 内 `pending_approval`；page >=1，page_size `1..100`，稳定排序 `created_at,id`。 |
| `POST /approvals/{proposal_id}/approve` | supervisor/admin | 请求含 submitted `revision_id`；允许自审；成功返回安全 `publish_record`。 |
| `POST /approvals/{proposal_id}/reject` | supervisor/admin | 请求含 revision_id 和 trim 后 `1..500` comment；终态 rejected，不改商品。 |
| `POST /approvals/{proposal_id}/request-changes` | supervisor/admin | 请求含 revision_id 和 trim 后 `1..500` comment；回 pending_manual，不改商品。 |
| `GET /audit-events` | supervisor/admin | 仅 scope 内事件；page/page_size 和可选 store/proposal/action filter 均有界。 |
| `GET /proposals/{id}` | operator/supervisor/admin | 扩展 current revision origin/number、manual run 安全状态、submitted revision、latest action 和 publish record。 |

`GET /proposals/{id}` 与 approvals/audit 响应不返回 idempotency/request/trusted fact hash、lease owner、checkpoint、input/output 原 JSON、Prompt、路径、向量、Authorization、Key 或 provider 原始响应。

### 7.3 状态规则

- 人工修订：仅 `draft_ready|pending_manual`；`pending_approval` 固定 `409/PROPOSAL_EDIT_FORBIDDEN`。
- submit：仅当前 revision 的 review 为 `normal + passed + error_code NULL`，且 Product version/citations 仍可信；否则 `409/PROPOSAL_NOT_SUBMITTABLE`。
- approve/reject/request_changes：仅 `pending_approval` 且请求 revision 等于 `submitted_revision_id`。
- reject/request_changes comment 为空、超长或含 Unicode control character固定 422。
- 同一 proposal 的不同活动人工修订请求固定 `409/MANUAL_REVIEW_ACTIVE`。
- supervisor/admin 可批准自己的 submit；不实现 maker-checker 分离。

## 8. Manual Review Worker graph, lease and recovery

### 8.1 独立 Worker 与 CLI

新增独立 Manual Review Worker 与 CLI；不向当前 optimization Worker 添加分支，不重构当前图。它只复用：

- `workflow_leases` 的 PostgreSQL server time、owner/type/unexpired guard 和 commit pattern
- 当前 trusted-input loader 与 canonical citation DB recheck
- `validate_optimization_output`
- `ProductComplianceAgentClient`

领取只选 `workflow_type='manual_review'` 的 `accepted` 或过期 `processing`，使用 `FOR UPDATE SKIP LOCKED`；领取是唯一增加 attempt count 的动作。`workflow_run.id` 是唯一 LangGraph `thread_id`。

### 8.2 六节点图

```text
load_or_resume
→ load_trusted_input
→ run_deterministic_checks
→ call_compliance_agent
→ persist_manual_review
→ finalize_manual_review
```

- `load_or_resume`：fresh-load owner、type、unexpired lease、actor/role/scope、proposal/manual run/revision/product/version/current pointer。
- `load_trusted_input`：外部 RAG 边界前续租；只接受 DB-current active/applicable canonical citations。
- `run_deterministic_checks`：对已持久化 manual output重新执行完整 validator。
- `call_compliance_agent`：只有 RAG quality normal 才调用；primary 后仅在 schema invalid 时最多一次 schema repair。每次 HTTP attempt 前续租。
- `persist_manual_review`：不可变写入 deterministic + semantic review、safe AgentCall；exact replay 返回既有行，non-exact race 传播数据库冲突或稳定 replay conflict。
- `finalize_manual_review`：锁 manual run、proposal、原 optimization run 和 Product，清 active pointer并原子写 terminal/audit。

现有 compliance client 的 Prompt、payload key、版本和 schema 不变。manual workflow 只有一次合规语义 pass，client 的调用局部 iteration 使用 `0`；proposal revision 与 compliance review 的业务 `iteration` 仍为 `NULL`，revision 身份只由 workflow/proposal_revision_id/revision_number 表示，绝不把 `0` 当人工版本号。

### 8.3 路由与降级

- `zero_hit` 或 `low_confidence`：零 DeepSeek POST；持久化 degraded failure review，manual run `completed/degraded`，原 optimization run `pending_manual/manual_review_degraded`。
- primary transport/HTTP/timeout/key failure：按现有 runtime 安全分类；不 schema repair；持久化一组安全 calls 与 degraded review。
- primary schema invalid：最多一次 schema repair；repair 失败后 degraded，不递归。
- deterministic 或 semantic 不通过但依赖正常：持久化 `normal/passed=false` review，manual run completed，原 run 保持 `pending_manual/manual_review_changes_required`。
- 两轨通过：manual run completed/normal，原 run `draft_ready/manual_review_passed`。
- Product version、事实/归属、授权变化、DB、checkpoint、immutable replay 错误：manual run failed，原 run failed，active pointer 在同一 owner-guard terminal 事务清理。

### 8.4 checkpoint 与旧 owner

checkpoint user channel 只允许 workflow/manual/proposal/revision ID、review ID、safe quality/error code、next node；不保存商品全文、trusted input、Prompt、provider response、Key 或 header。每个 DB 写和每个 RAG/HTTP 外部边界前重新验证 `workflow_type=manual_review`、`processing`、owner、未过期 lease、proposal active pointer、Product base version。

`asyncio.CancelledError` 原样传播并保留 processing lease。过期后新 owner 以同 thread ID 恢复；已存在 exact review/call/audit 只重放，不重复写。旧 owner、owner replacement、lease expiry 或 checkpoint cancellation 后零外部调用、零 revision/review/call/pointer/terminal 写入。fresh failure path 若再次数据库失败，原始数据库异常传播，不伪造 terminal。

## 9. Approval and publish transaction

### 9.1 submit

submit 在一笔事务内锁 actor/scope、proposal、原 optimization run、current revision/review 和 Product；确认 `draft_ready`、current revision 即请求 revision、review `normal+passed`、无 active manual run、Product version 等于 base version。随后写 append-only submit action、`proposal_submitted` audit，设置 `submitted_revision_id`，把原 run 置 `pending_approval`。失败全回滚。

### 9.2 approve 的单一锁定事务

approve 固定执行以下顺序：

1. 以稳定顺序锁 actor、精确 scope、proposal、原 optimization run、submitted revision、唯一 compliance review、Product；读取并锁相关 SKU 仅用于当前事实校验，不更新 SKU。
2. 确认状态 `pending_approval`、请求 revision 等于 `submitted_revision_id/current_revision_id`、无 active manual run、review 为 `normal+passed+no error`。
3. 确认 `Product.current_version == proposal.base_product_version == revision.base_product_version`，并重新验证 inherited canonical citations 和当前 SKU 事实。
4. 将结构化 description确定性渲染为 Text：每节为 `heading + "\n" + body`，节间用 `"\n\n"`；保存渲染后字符串。
5. 计算允许字段的 before snapshot；更新 Product `title/selling_points/description/search_keywords`，并把每个已验证 attribute completion 合并到当前 attributes；除此之外不更新任何列。
6. 设置 `Product.current_version = base + 1`，计算 after snapshot。
7. 写唯一 approve action、唯一 publish record、`proposal_approved` 与 `simulated_publish_completed` audit。
8. 最后把原 optimization run 写为 `completed/normal/current_step=simulated_published`，清 error；提交一次事务。

任一步异常都 rollback，所以不会出现版本已递增但缺 publish record、或 publish record 存在但商品未更新的半状态。

### 9.3 replay、并发与其他动作

- 相同 approve key/request hash 返回既有 action 与 publish record。
- proposal 已 completed 时，对同 submitted revision 的重复 approve 即使使用新 key，也只返回唯一 publish record，不新增 action/audit、不递增版本。
- 同时 approve/approve 由 proposal/Product 锁和 publish unique constraints收敛为一次发布；后到者返回同 record。
- approve 与 reject/request_changes 并发时，先取得锁并提交者获胜；冲突动作返回 `409/APPROVAL_ACTION_CONFLICT`，零额外写入。
- Product version 已变化返回 `409/PRODUCT_VERSION_CONFLICT`，零 action/publish/audit/Listing 写入。
- reject：写 action/audit，原 run `rejected`；Product、revision、review、版本不变。
- request_changes：写 action/audit，原 run `pending_manual/normal/approval_changes_requested`，清 `submitted_revision_id`；Product 和版本不变。

## 10. Audit, security and error codes

### 10.1 安全边界

- API 进程不读取或调用 DeepSeek Key。
- API、Worker、checkpoint、业务表、audit 和响应不保存或返回 Key、Authorization、Cookie、完整 Prompt、原始响应、思维链、路径、向量或未脱敏异常。
- `agent_calls` 继续只保存模型、Prompt 版本、node/call type、call-local iteration/attempt、input hash、token、耗时、成本和固定 error code。
- audit details 只接受第 5.8 节闭集；任何未知 key 在写入前拒绝，不自动透传请求体或异常对象。
- comment 是受限业务意见，不进入 audit details；请求层拒绝 control characters。
- 所有 SQL 参数化，客户端不能提供 store、product、workflow owner、路径或 SQL。

### 10.2 稳定 API 错误码

| HTTP | code | 含义 |
|---|---|---|
| 400 | `IDEMPOTENCY_KEY_INVALID` | 缺失、空白或超过 128。 |
| 403 | `PROPOSAL_ACTION_FORBIDDEN` | 当前角色不能执行该动作。 |
| 404 | `PROPOSAL_NOT_FOUND` | 未知、跨店、scope 缺失或资源归属不可见。 |
| 409 | `IDEMPOTENCY_REPLAY_CONFLICT` | 同 key 不同规范请求。 |
| 409 | `MANUAL_REVIEW_ACTIVE` | 已有不同活动人工复核。 |
| 409 | `PROPOSAL_EDIT_FORBIDDEN` | 当前状态不可编辑。 |
| 409 | `PROPOSAL_NOT_SUBMITTABLE` | revision/review/quality 不可提交。 |
| 409 | `APPROVAL_STATE_CONFLICT` | 非 pending_approval 或 stale submitted revision。 |
| 409 | `APPROVAL_ACTION_CONFLICT` | 并发冲突动作已有胜者。 |
| 409 | `PRODUCT_VERSION_CONFLICT` | 商品版本不再等于 proposal base。 |
| 409 | `PUBLISH_REPLAY_CONFLICT` | existing immutable publish 与请求不精确一致。 |
| 422 | `MANUAL_REVISION_INVALID` | typed/长度/结构化变更不合法。 |
| 422 | `TRUSTED_EVIDENCE_INVALID` | fact/citation/current-value allowlist 失败。 |
| 422 | `APPROVAL_COMMENT_INVALID` | reject/request_changes 意见不合规。 |
| 503 | `PROPOSAL_DATA_INCONSISTENT` | 数据库归属链或 immutable 指针损坏。 |

### 10.3 Manual Worker 固定错误码

事实失败闭集：`MANUAL_REVIEW_CONTEXT_NOT_FOUND`、`MANUAL_REVIEW_CONTEXT_INCONSISTENT`、`MANUAL_REVIEW_AUTHORIZATION_CHANGED`、`PRODUCT_VERSION_CONFLICT`、`MANUAL_REVIEW_FACT_ERROR`、`MANUAL_REVIEW_DATABASE_ERROR`、`MANUAL_REVIEW_CHECKPOINT_ERROR`、`MANUAL_REVIEW_REPLAY_CONFLICT`。

依赖降级复用现有安全闭集：`DEEPSEEK_KEY_MISSING`、`DEEPSEEK_TIMEOUT`、`DEEPSEEK_TRANSPORT`、`DEEPSEEK_RATE_LIMIT`、`DEEPSEEK_SERVER_ERROR`、`DEEPSEEK_UNAUTHORIZED`、`DEEPSEEK_FORBIDDEN`、`DEEPSEEK_HTTP_ERROR`、`DEEPSEEK_SCHEMA_INVALID`、`KNOWLEDGE_MODEL_UNAVAILABLE`、`KNOWLEDGE_DEPENDENCY_TIMEOUT`、`KNOWLEDGE_DEPENDENCY_ERROR`、`KNOWLEDGE_ZERO_HIT`、`KNOWLEDGE_LOW_CONFIDENCE`、`COMPLIANCE_AGENT_DEGRADED`。

## 11. Test matrix and acceptance

### 11.1 普通离线测试

| 层 | 必须覆盖 |
|---|---|
| Schema/migration | 自动 revision 回填 number/origin/creator/parent；manual iteration NULL；review nullable；全部 FK/UNIQUE/CHECK/index；旧数据可读。 |
| API/RBAC | operator 修订/提交但审批 403；supervisor/admin 可修订、提交、自审；三角色精确 scope；disabled user/store、跨店和资源归属拒绝。 |
| Manual input | 五类可编辑字段；citations/price/SKU 键 extra-forbid；只读字段安全继承；fact path/citation/current-value allowlist；stale parent/version。 |
| Idempotency | 每个写 API 缺 key、exact replay、same-key different-body；竞态只一行；hash 不响应、不日志。 |
| Worker | 类型隔离；绝不调用 optimization Agent；RAG normal/zero_hit/low_confidence/timeout/error；compliance primary + 最多一 repair；safe calls。 |
| Lease/recovery | claim、attempt cap、每个外部边界续租、旧 owner、owner replacement、CancelledError、checkpoint get/put failure、same thread exact resume。 |
| Review/state | deterministic/semantic 两轨；passed 回 draft_ready；valid non-pass/degrade 回 pending_manual；fatal failed；active pointer原子创建/清理。 |
| Approval | submit、self-approve、reject、request_changes、重走 manual review；pending_approval 禁编辑；冲突动作稳定 409。 |
| Publish | 单一事务、exact replay、并发批准、版本冲突、rollback fault injection；version只加一次；Listing映射正确。 |
| 不可变事实 | 价格、SKU code/spec、current_stock、inventory snapshots 全部不变；只读建议原样保留。 |
| Audit/security | append-only；details exact allowlist；响应/checkpoint/table/log 不含 Key/Auth/Prompt/raw/thought/path/vector/hash。 |

普通 Agent/RAG 测试全部使用 fake 或 `httpx.MockTransport`，清空 DeepSeek/PostgreSQL/RAG opt-in 与 Key，不访问网络、不加载模型或 Milvus。

### 11.2 PostgreSQL opt-in

显式 opt-in 验证真实 `FOR UPDATE SKIP LOCKED` claim、过期租约恢复、旧 owner 零写、同 proposal 单 active manual run、immutable race exact replay/non-exact error、并发 submit/approve/reject/request_changes、商品版本冲突、批准事务回滚和迁移回填。测试记录全部新增 ID，按 FK 顺序精确清理；不得清理非测试数据。

### 11.3 纵向验收

1. `pending_manual` proposal 经人工 revision、独立复核双轨通过、submit、supervisor/admin 自审 approve，生成唯一 publish record，Product 允许字段更新且 version 恰好 +1。
2. `request_changes` 不改商品，回 `pending_manual`；新人工 workflow 使用新 ID/thread，复核通过后可再次 submit/approve。
3. `reject` 终态且不改商品。
4. 每条流均证明 price、SKU、stock、inventory 不变，audit 可追溯且无敏感字段。
5. 完整门禁：普通全量 `pytest`、`compileall backend tests scripts`、Alembic `current/check`、`docker compose config --quiet`、`git diff --check` 和精确 Git 状态。

## 12. Compatibility and non-implemented boundaries

- 现有 analysis、knowledge、selection、optimization Agent、compliance Agent、runtime、RAG API 和 optimization Worker 的行为保持不变。
- 现有 proposal revision iteration `0..2`、review iteration `0..2` 和 AgentCall 审计继续可读；迁移只添加通用 `revision_number` 和 manual nullable 路径，不重写自动历史。
- Manual Review Worker 是独立领取函数、独立图、独立 CLI；只复用稳定 primitives 和 clients，不引入共享 base Worker、provider factory 或第二套 validator。
- 本阶段实现后只能声称“本地 PostgreSQL 模拟发布”；不能声称已接真实电商平台、已实现前端、已应用价格/SKU建议、已新增价格权限或已上线生产。
- 不新增真实 DeepSeek smoke，因为现有 Prompt/client 不变；新编排的模型边界由 MockTransport 覆盖。

## 13. Acceptance summary

本阶段被接受的必要条件是：一份人工 revision 对应一个新 manual workflow；同 proposal 只有一个 active manual review；两轨复核通过才可提交；主管动作实时授权且幂等；approve 在单一事务中只发布一次；request_changes 可重新进入独立人工复核；reject 不改商品；价格、SKU、库存始终不变；全部审计只追加且安全；所有离线、PostgreSQL、迁移、Compose 和 Git 门禁实际通过。任何前端、真实平台、价格/SKU应用或未经验证的模型能力仍明确属于未实现范围。
