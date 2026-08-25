# 智营台（E-commerce Operations）需求与架构设计

> 状态：需求已确认，等待用户书面复核<br>
> 日期：2026-08-25<br>
> 项目路径：`D:\E-commerce_operations`<br>
> 当前实现状态：空项目，本文所有能力均为目标设计，不代表已经实现

## 1. 项目目标

建设一个面向国内货架电商公司的多店铺 AI 智能运营平台。系统使用经营数据识别问题商品，由多个专业 Agent 完成诊断、商品信息优化和合规审核，再经过运营与主管的人工作业节点完成本地模拟发布。

项目服务于 AI Agent 开发工程师岗位展示，重点证明以下能力：

- LangGraph 多 Agent 编排、条件循环与 Human-in-the-Loop。
- RAG 文档版本、混合检索、重排、引用和低置信度处理。
- 结构化工具调用、稳定 Schema、局部重试和降级。
- 持久任务、检查点恢复、幂等发布、RBAC 和审计。
- 离线评测、在线指标、故障注入和端到端验证。

项目只在本地通过 Docker Compose 演示，不要求上线，也不接入淘宝、京东等真实平台。

## 2. 项目边界

### 2.1 核心范围

- 一家公司管理多个国内货架电商店铺。
- 内置可重复生成的商品、SKU、订单、流量、库存、退款和退货数据。
- 经营分析 Agent 推荐问题商品，运营选择一个商品继续处理。
- 商品优化 Agent 生成中文标题、卖点、详情、搜索关键词和属性补全。
- 合规审核 Agent 独立审核，并可最多自动退回修订两次。
- 运营查看差异并提交主管审批；主管批准后执行本地模拟发布。
- 管理员维护平台规则、禁限词、类目规范和企业文案规范知识库。
- 提供经营、Agent 质量、审批、审计和工作流追踪页面。

### 2.2 明确不做

- 不接入真实电商平台、支付、物流或真实订单履约。
- 不声称实现真实销售额或转化率提升。
- 不生成商品图片、视频或直播脚本。
- 不做定时经营分析，任务由运营手动发起。
- 不做多商户 SaaS 和跨公司租户体系。
- 不使用 Qwen3:8b 或其他本地生成式大模型。
- 不允许 AI 自动发布商品。
- 首版不引入微服务、MCP、Redis、Celery、Kafka 或业务对象存储。
- 首版不使用互联网搜索。

## 3. 用户与权限

| 能力 | 运营专员 | 运营主管 | 系统管理员 |
|---|---:|---:|---:|
| 查看授权店铺 | 是 | 是 | 是 |
| 发起经营分析 | 是 | 是 | 是 |
| 选择商品并生成优化方案 | 是 | 是 | 是 |
| 查看差异并提交审批 | 是 | 是 | 是 |
| 批准、驳回或要求修改 | 否 | 是 | 是 |
| 管理知识库 | 否 | 否 | 是 |
| 管理用户和店铺授权 | 否 | 否 | 是 |
| 查看完整审计日志 | 否 | 是 | 是 |

认证只确认身份。每个资源接口还必须校验角色、店铺授权、资源归属和当前业务状态。前端按钮或路由守卫不能替代服务端鉴权。

## 4. 目标架构

```text
Vue 3 + TypeScript 运营管理台
                │ REST + 任务状态轮询
                ▼
FastAPI 模块化后端
├── JWT / RBAC / 店铺授权
├── 商品、订单、库存、知识库 API
├── 工作流创建、查询、人工操作 API
└── 幂等模拟发布工具
                │ 创建持久任务
                ▼
PostgreSQL workflow_runs ← 独立 Worker 领取任务
                                 │
                                 ▼
                         LangGraph 编排器
                   ┌─────────────┼─────────────┐
                   ▼             ▼             ▼
              经营分析 Agent  商品优化 Agent  合规审核 Agent
                   │             │             │
                SQL 工具      商品/RAG 工具   规则/RAG 工具
                   └─────────────┼─────────────┘
                                 ▼
                            DeepSeek API

PostgreSQL：业务事实、任务租约、检查点、审批、审计、评测
Milvus：规则知识库 dense/sparse 检索索引
本地挂载目录：管理员上传的原始知识文档
```

### 4.1 技术选型

| 层 | 采用 | 理由 |
|---|---|---|
| 前端 | Vue 3、TypeScript、Element Plus | 复用已有经验，适合运营管理台 |
| API | Python 3.11、FastAPI、Pydantic v2 | 异步 API 和结构化边界成熟 |
| 工作流 | LangGraph | 需要条件循环、暂停、恢复和检查点 |
| LLM | DeepSeek OpenAI 兼容 API | 不依赖本地生成模型，中文能力和成本合适 |
| 业务事实 | PostgreSQL、SQLAlchemy asyncio | 事务、约束、幂等和审计的事实源 |
| 检查点 | LangGraph PostgreSQL Checkpointer | 人工作业节点必须跨重启恢复 |
| RAG | BGE-M3、轻量 Reranker、Milvus | 支持中文 dense/sparse 混合召回与精排 |
| 后台任务 | PostgreSQL 租约表 + 独立 Worker | 满足本地持久任务，不增加 Redis/Celery |
| 部署 | Docker Compose | 本地可复现、无需云环境 |
| 测试 | pytest、HTTPX、Playwright | 覆盖单元、集成、接口和核心浏览器链路 |

Milvus 所需的 etcd/MinIO 仅作为其基础设施依赖，不对外宣称为业务对象存储能力。

## 5. Agent 与工具职责

### 5.1 经营分析 Agent

允许调用以下只读工具：

- `get_store_summary`
- `find_anomalous_products`
- `get_product_metrics`
- `compare_store_products`
- `get_inventory_risk`

工具使用固定 Pydantic 参数并由服务端注入可信 `store_id`，不允许模型生成任意 SQL。曝光、点击率、转化率、销量趋势、库存周转、缺货风险和退款/退货率均由确定性代码计算。Agent 只负责解释、影响评估、排序和建议。

输出至少包含：`product_id`、`rank`、`anomaly_types`、`business_impact`、`reason`、`recommended_action`、`evidence` 和 `confidence`。

### 5.2 商品优化 Agent

输入包括商品当前版本、SKU、经营诊断、同店高表现商品摘要以及 RAG 规则。输出必须通过 Pydantic Schema：

- `title`
- `selling_points[]`
- `description`
- `search_keywords[]`
- `completed_attributes`
- `changes[]`，含字段、修改前、修改后和原因
- `cited_sources[]`

模型不得编造商品参数。任何新增事实必须来自商品数据、确定性工具或有来源的知识。

### 5.3 合规审核 Agent

采用双轨审核：

1. 确定性规则检查禁限词、长度、缺失属性、规格和价格冲突。
2. LLM 检查夸大宣传、误导表达、语义冲突和证据不足。

输出包含 `passed`、`risk_level`、`violations[]`、`required_changes[]`、`cited_sources[]`、`confidence` 和 `degraded`。审核 Agent 不直接修改商品内容，只把问题返回商品优化 Agent。

## 6. 工作流设计

### 6.1 经营分析工作流

```text
校验用户与店铺权限
→ 加载指定店铺和日期范围的数据
→ 确定性计算指标与异常信号
→ 经营分析 Agent 解释并排序
→ 保存候选商品和证据
→ completed，等待运营选择商品
```

运营选择候选商品是业务级 Human-in-the-Loop，但不保持一个长期运行的分析图；选择动作会创建新的商品优化工作流。

### 6.2 商品优化与发布工作流

```text
加载商品版本和诊断
→ 检索商品资料、类目规范和文案规则
→ 商品优化 Agent 生成方案
→ 合规审核 Agent 独立审核
    ├── 通过：保存 draft
    └── 不通过：携带问题退回商品优化 Agent
          ├── revision_count < 2：重新生成
          └── revision_count = 2：pending_manual

运营查看差异并提交
→ pending_approval
→ 主管处理
    ├── approve：幂等模拟发布 → completed
    ├── reject：rejected
    └── request_changes：返回 draft
```

`pending_manual` 时，运营可创建人工修订版本并重新进入合规审核。所有人工修改和重新提交都写入审计记录。

### 6.3 生命周期与质量状态

生命周期状态：

```text
accepted | processing | awaiting_selection | draft | pending_manual |
pending_approval | completed | rejected | failed | cancelled
```

质量状态与生命周期分离：

```text
quality.status = normal | partial | degraded
quality.missing_parts[]
quality.fallback_reason
```

这避免把“任务已完成但结果不完整”和“任务仍在处理中”混为一谈。

## 7. 持久任务、恢复和幂等

`workflow_runs` 至少记录：

- `workflow_type`、`status`、`current_step`
- `input`、`output`、`quality`、`error`
- `attempt_count`
- `lease_owner`、`lease_expires_at`、`heartbeat_at`
- `created_by`、`store_id`、时间戳

Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取任务，执行时续租并保存 PostgreSQL Checkpointer。Worker 退出后，租约到期的任务可重新领取。

重试规则：

- DeepSeek 超时、限流和临时 5xx：指数退避，最多重试两次。
- 输入、权限、Schema 和业务冲突：不重试。
- 只重试最小幂等外部调用，不重试含数据库写入或发布动作的整张图。
- 达到最大尝试次数后进入 `failed`。

模拟发布使用 `proposal_id + proposal_version` 生成唯一幂等键。同一商品使用乐观版本锁，基于旧商品版本的方案不能覆盖新版本。

## 8. 数据模型

### 8.1 业务事实

- `users`：角色和启用状态。
- `stores`：店铺基础信息。
- `user_store_scopes`：用户可访问店铺。
- `products`：当前 Listing、属性、类目和版本。
- `product_skus`：规格、价格和当前库存。
- `orders`、`order_items`：订单、商品、金额和退款/退货状态。
- `traffic_daily`：曝光、点击、访客和加购。
- `inventory_snapshots`：每日库存和在途数量。

### 8.2 AI 工作流

- `workflow_runs`
- `analysis_candidates`
- `optimization_proposals`
- `approval_actions`
- `publish_records`
- LangGraph Checkpointer 自有表

### 8.3 知识、评测与审计

- `knowledge_documents`
- `knowledge_chunks` 元数据，向量本体存入 Milvus
- `agent_calls`
- `evaluation_cases`
- `evaluation_results`
- `audit_events`

关键约束：店铺内商品编码唯一、发布幂等键唯一、知识文件哈希与版本唯一；价格、库存、分数和修订次数使用数据库 `CHECK`；所有外键和资源归属在数据库与服务层同时校验。

## 9. RAG 设计

```text
管理员上传文档
→ 文件边界和解析质量检查
→ 按标题与段落分块
→ 稳定 document_id + version
→ BGE-M3 dense/sparse 向量
→ Milvus 幂等 upsert
→ 查询时混合召回
→ Reranker 精排
→ 置信度路由
→ 返回文本、来源和版本
```

要求：

- 支持 PDF、DOCX、Markdown 和 TXT。
- 文档更新后停用旧版本并删除或隔离旧分块。
- 区分 `zero_hit`、`low_confidence`、`timeout` 和 `dependency_error`。
- 检索结果保留文档名、版本、chunk ID、得分和索引版本。
- 阈值、top-k 和融合权重通过离线评测校准，不复制经验常量。
- 知识文档视为不可信数据，不得覆盖系统指令或触发未授权工具。

## 10. LLM 边界

项目只有 DeepSeek 一个生成供应商，首版使用简单统一的 `LLMClient`，不创建多供应商工厂。

- 模型名、温度、超时和 API Key 来自配置。
- 关闭 SDK 内部重试，由工作流统一控制。
- 结构化输出先做 Schema 验证，可格式修复或重新调用一次。
- 仍失败时返回相同业务 Schema，并标记 `degraded`。
- 每次调用记录模型、Prompt 版本、Token、耗时、估算成本、重试和降级原因。
- 不要求或保存隐藏思维链。

## 11. API 契约

主要接口：

```text
POST /auth/login

GET  /stores
GET  /stores/{id}/products

POST /analysis-runs
GET  /workflow-runs/{id}
GET  /analysis-runs/{id}/candidates
POST /analysis-runs/{id}/select-product

GET  /proposals/{id}
POST /proposals/{id}/submit
POST /proposals/{id}/manual-revision

POST /approvals/{id}/approve
POST /approvals/{id}/reject
POST /approvals/{id}/request-changes

POST /knowledge/documents
GET  /knowledge/documents
POST /knowledge/documents/{id}/versions
POST /knowledge/documents/{id}/disable

GET  /dashboards/business
GET  /dashboards/agent-quality
GET  /audit-events
```

耗时任务返回 HTTP 202 和 `workflow_run_id`。前端递归轮询统一状态接口，上一请求完成后才安排下一次请求，页面卸载时停止轮询。

统一响应包含 `request_id`、`status`、`data`、`quality` 和安全的 `error`。内部异常堆栈不返回客户端。

## 12. 前端信息架构

首页采用“任务工作台优先”，突出需要继续处理的 Agent 任务，而不是传统 BI 指标堆叠。

侧边导航：

- 工作台
- 经营分析
- 优化任务
- 审批中心
- 知识库
- Agent 评测
- 审计日志
- 系统管理

核心任务详情显示五阶段：经营分析、运营选品、AI 优化、主管审批、模拟发布。候选商品列表同时展示排序、异常类型、影响级别和可追溯证据。

## 13. 安全要求

- JWT 密钥和 DeepSeek API Key 只来自环境变量。
- 日志不记录密钥、密码、完整 Token、上传文档原文或完整 Prompt。
- 上传校验扩展名、MIME、魔数、大小、页数、加密状态和解析超时。
- SQL 参数化，工具不接受任意 SQL、文件路径或完整可信标识。
- thread/workflow ID 由服务端生成并结合身份检查。
- 审批、驳回、人工修改、知识更新、权限拒绝和发布写入只追加的审计事件。
- 客户端使用稳定错误码；内部错误只进入受控日志。

## 14. 评测与可观测性

离线评测覆盖经营异常、Listing 质量、合规审核和 RAG 引用。LLM Judge 只辅助评价文案质量，事实、字段、禁限词和引用使用确定性程序优先评测。

| 指标 | 首版目标 |
|---|---:|
| 问题商品 Precision@5 | ≥ 80% |
| Listing 结构完整率 | 100% |
| 商品事实一致率 | ≥ 95% |
| 明确禁限词召回率 | 100% |
| 语义合规风险召回率 | ≥ 85% |
| RAG 引用正确率 | ≥ 90% |
| 工作流正常完成率 | ≥ 95% |
| 重复模拟发布次数 | 0 |
| 审批跨重启恢复测试 | 100% 通过 |
| 降级结果 Schema 合法率 | 100% |

在线记录运营采纳率、草稿提交率、主管通过/驳回/修改率、平均自动修订次数、完成时长、节点耗时、Token、估算成本、Prompt 版本、工具调用状态、检索来源、重试和降级原因。

首版将追踪数据写入 PostgreSQL 并在管理台展示，不额外部署 Langfuse。

## 15. 测试设计

### 15.1 单元测试

- 经营指标、异常规则和候选排序。
- Agent 输入输出 Schema。
- 工作流条件边、两次修订上限和终止状态。
- RBAC、店铺授权和资源归属。
- 可重试异常分类、发布幂等和乐观锁。
- 知识文档版本、哈希去重和引用结构。

### 15.2 集成测试

- FastAPI、PostgreSQL 权限与事务。
- Worker 租约、心跳、超时重领和最大尝试次数。
- LangGraph 暂停、人工恢复和服务重启恢复。
- Milvus 写入、更新、停用、零命中和故障状态。
- DeepSeek Mock Server 的超时、限流、格式错误和降级。
- 重复审批和发布不产生重复副作用。

### 15.3 端到端测试

核心浏览器链路：运营登录、发起分析、选择商品、生成方案、合规修订、提交审批、主管批准、模拟发布、查看审计与评测记录。

另行覆盖主管驳回、连续两次合规失败、Worker 中断恢复和知识库故障。前端必须通过 TypeScript `noEmit`、生产构建、关键状态组件测试和一条 Playwright 核心链路。

## 16. 实施顺序

1. 工程地基、配置、数据库迁移、认证和店铺权限。
2. 固定种子的模拟业务数据与确定性经营指标工具。
3. PostgreSQL 持久任务、Worker 租约和检查点。
4. 经营分析 Agent 与候选商品工作流。
5. 知识库导入、Milvus 混合检索和引用。
6. 商品优化 Agent、合规审核 Agent 和两次修订循环。
7. 运营提交、主管审批、幂等模拟发布和审计。
8. 任务工作台、经营分析、优化和审批前端。
9. 知识库、Agent 评测、审计和系统管理页面。
10. 故障注入、端到端测试、评测报告和文档核对。

## 17. 最终验收标准

1. Docker Compose 能启动完整本地环境。
2. 一条初始化命令生成稳定可重复的多店铺演示数据。
3. 三种角色只能访问授权功能与店铺资源。
4. 三个 Agent 的输入、输出、工具和评测结果可独立查看。
5. 主业务闭环能从经营分析稳定执行到模拟发布。
6. 合规失败最多自动修订两次，不出现无限循环。
7. RAG 内容显示真实文档来源和版本。
8. DeepSeek 或 Milvus 故障时明确显示失败、部分成功或降级。
9. Worker 重启后任务可继续，主管审批跨重启不丢失。
10. 重复提交、审批和发布不产生重复数据。
11. 离线评测达到第 14 节指标。
12. 后端测试、前端类型检查、生产构建和核心 E2E 实际通过。
13. README、架构图、接口、数据库模型、依赖和 Compose 与真实代码一致。
14. 简历只陈述真实完成并验证的能力。

## 18. 初始假设

- 单机本地演示，不以高并发或多实例为验收目标。
- 默认生成 3 个店铺、约 300 个商品、若干 SKU 和近 90 天经营数据；规模可通过种子脚本参数调整。
- 商品类目是演示数据，不把核心架构绑定到单一类目。
- DeepSeek API 可用且用户自行提供密钥。
- BGE-M3 和 Reranker 可在本机作为检索模型运行，不承担生成任务。
- 知识原文件使用 Docker 挂载目录保存，单机模式不需要业务对象存储。

## 19. 证据等级与文档真实性

当前目录为空且不是 Git 仓库。因此本文所有目标能力的当前证据等级均为 D：尚未实现。后续只有满足“代码存在、被真实入口调用、数据流闭环并通过验证”的能力才能升级为 A。

实施过程中必须持续记录以下差异：

- 设计已实现的部分。
- 代码存在但未接主入口的部分。
- 只有配置或文档声明的部分。
- 占位、空实现或被删除的部分。

禁止将设计目标、计划卡片、进程内状态或模拟发布描述成真实平台集成和生产能力。
