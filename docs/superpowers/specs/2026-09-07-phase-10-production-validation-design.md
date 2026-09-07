# Phase 10 Production Validation and Delivery Design

日期：2026-09-07
项目路径：`D:\E-commerce_operations`
上游：[总设计](2026-08-25-ecommerce-operations-design.md)、[商品优化与合规设计](2026-08-28-product-optimization-compliance-design.md)、[人工修订、审批与模拟发布设计](2026-08-31-manual-review-approval-publish-design.md)、[运营管理台前端设计](2026-09-01-frontend-operations-console-design.md)、[阶段九管理控制台设计](2026-09-04-phase-9-management-console-design.md)

## 1. Goal

阶段十完成当前项目的生产化验收，而不是继续扩张业务功能：

- 使用真实 DeepSeek 验证分析、商品优化和合规 Agent 的受控契约。
- 使用真实本机 FastAPI、PostgreSQL、Milvus、本地 BGE 模型和 Worker 运行完整业务闭环。
- 增加一个通用、契约级的本地电商平台模拟器，验证生产系统常见的认证、限流、幂等、Webhook 和故障恢复语义。
- 将已审批的本地模拟发布通过持久投递任务同步到平台模拟器，同时保持现有价格、SKU 和库存只读边界。
- 完成故障注入、真实服务浏览器 E2E、最终验证报告和根 README。

验收结论必须准确区分：真实 DeepSeek 和真实本机基础设施已经运行；电商平台仅完成契约模拟，尚未取得任何真实平台的商家认证或正式 API 联调。

## 2. Non-goals

阶段十不做以下事项：

- 不连接淘宝、天猫、京东、拼多多、抖音电商或其他正式商家店铺。
- 不声称通过任何真实电商平台的应用审核、OAuth 授权或生产接口认证。
- 不发布真实商品，不修改真实价格、SKU、库存或订单。
- 不建立微服务边界，不增加消息队列、缓存、服务网格、状态管理库或测试框架。
- 不实现无法用当前契约模拟验证的企业密钥托管、每店 OAuth 授权后台或 Token 加密轮换系统。
- 不读取、修改或暂存 `docs/business-and-technical-guide.md` 与 `docs/interview-q-and-a.md`。

## 3. Architecture

系统继续保持现有 FastAPI 单体和独立 Worker 进程。阶段十复用 SQLAlchemy、PostgreSQL、httpx、FastAPI、现有租约模式和 Playwright，不增加运行时依赖。

```text
主管/管理员批准方案
        │
        ▼  单一 PostgreSQL 事务
PublishRecord + PlatformDelivery(pending) + safe audit
        │
        ▼  租约领取，事务外 HTTP
Platform Delivery Worker ──> CommercePlatformClient
                                  │
                                  ▼
                        Local Contract Simulator
                        OAuth / idempotency / 429
                        timeout / 5xx / webhook
                                  │
                                  ▼
                     safe status + audit metadata
```

批准事务不执行网络请求。现有 `PublishRecord` 仍是本地、不可变的发布事实；平台投递是其独立的最终一致性副作用。平台不可用不能回滚已经提交的主管批准，也不能造成重复平台写入。

现有工作流在本地批准成功事务完成后仍保持 `completed / simulated_published` 语义，以兼容第七至第九阶段。平台投递状态通过发布记录的安全扩展视图单独展示，不重新解释旧工作流或旧发布记录。

## 4. Durable platform delivery

### 4.1 PlatformDelivery

一个新的 `platform_deliveries` 表承担持久 outbox 和执行状态，不复制商品快照。请求内容始终从关联的不可变 `PublishRecord.after_snapshot` 构造。

最小字段为：

- `id`：`String(36)` 主键。
- `publish_record_id`：不可空且唯一的 `PublishRecord` 外键。
- `store_id`：不可空店铺外键，用于授权和审计。
- `provider`：阶段十固定为 `contract_simulator`。
- `status`：闭集 `pending`、`processing`、`succeeded`、`failed`。
- `attempt_count`：非负整数，最多三次平台提交尝试。
- `next_attempt_at`：可空 UTC 时间，用于有界退避。
- `lease_owner`、`lease_expires_at`：同时为空或同时存在；仅 `processing` 可持有租约。
- `external_operation_id`：成功后保存的安全外部操作 ID，不保存响应正文。
- `error_code`：可空安全闭集短码。
- `created_at`、`updated_at`、`completed_at`：UTC 时间戳。

数据库至少约束发布记录唯一、状态闭集、尝试次数范围、租约一致性以及成功状态必须有 `external_operation_id` 和 `completed_at`。索引覆盖待领取任务和店铺时间线。阶段十只创建一个迁移 `0007_phase10_production_validation.py`。

### 4.2 Approval compatibility

新批准在现有原子事务中继续写入 `ApprovalAction`、`PublishRecord`、商品安全字段、工作流终态和原有审计，并额外写入一条 `pending` 投递任务及 `platform_delivery_enqueued` 审计。

旧 `PublishRecord` 不回填投递任务；历史仍可读取。相同批准或幂等重放不得产生第二条投递任务。平台投递只包含：

- `title`
- `selling_points`
- `description`
- `keywords`
- `attribute_completions`

价格、SKU、库存、订单和真实发布继续不可写。

### 4.3 Worker lifecycle

Worker 使用数据库时间、`FOR UPDATE SKIP LOCKED` 和现有租约风格领取到期的 `pending` 任务。网络请求在领取事务提交后执行。

- 成功：保存同一个外部操作 ID，状态变为 `succeeded`，追加安全成功审计。
- Token 过期：刷新一次 Token 后重试当前请求，不扩大总尝试上限。
- `429`、超时、连接失败和 `5xx`：按安全短码记录；未达到三次时回到 `pending` 并设置有界 `next_attempt_at`，否则进入 `failed`。
- 非重试型 `4xx`、响应契约错误或签名错误：直接进入 `failed`。
- Worker 在平台已接收、但本地成功提交前崩溃：租约到期后使用同一幂等键重放；模拟器返回同一个外部操作 ID。
- 失去租约的 Worker 不得写终态；完成或失败后清空租约。

错误、日志、审计和 API 响应不得包含访问令牌、刷新令牌、Client Secret、Authorization header、请求正文、响应正文或内部异常文本。

## 5. Contract simulator and client

本地平台模拟器是测试设施，不是新的产品服务。它使用现有 FastAPI 与标准库，在 E2E 启动脚本中作为独立本机进程运行，并持有确定性的进程内测试状态。

它只实现阶段十需要的最小契约：

- Client Credentials 风格的短期访问令牌和一次刷新路径。
- 店铺范围的商品更新接口。
- `Idempotency-Key`：相同 key 与相同请求返回同一操作 ID；相同 key 与不同请求返回冲突。
- 确定性分页响应，用于验证商品、订单和库存的只读契约。
- 可控的 `401`、`429 + Retry-After`、超时、`5xx`、响应 schema 错误和“远端提交后连接断开”。
- HMAC-SHA256 Webhook，包含时间戳、事件 ID 和固定重放窗口。

Webhook API 只接受 `publish.confirmed`。服务端验证时间戳和签名，在 `platform_webhook_receipts` 中保存唯一事件 ID、关联投递 ID、事件类型、payload digest 和接收时间；不保存原始 payload。重复事件返回幂等成功，不产生第二次业务写入或审计。Webhook 仅提供确认与证据，不改变已成功的平台投递结果。

`CommercePlatformClient` 是一个具体的 httpx 客户端，不创建插件系统或多 provider factory。它从环境配置读取模拟器 base URL、Client ID 和 Secret；Token 仅存在于进程内存。未来取得真实平台权限时，再以真实平台认证和字段映射替换这一薄边界。

## 6. Real DeepSeek validation

真实 DeepSeek 只在显式 `RUN_PHASE10_DEEPSEEK=1` 时运行，并要求本机安全配置中存在非空 Key。普通 pytest、浏览器 E2E、平台模拟器和故障注入默认都不访问 DeepSeek 或其他外网。

真实验证使用固定的最小合成可信输入：

- 分析 Agent：验证候选集合、商品与名次对应关系及安全调用元数据。
- 商品优化 Agent：验证闭合输出 schema、可信事实边界和 canonical citation。
- 合规 Agent：分别验证 deterministic、semantic contract 和 citation。

每个 Agent 最多一次 primary 和一次 schema repair；不执行不受控重试。安全结果只包含 Agent 类型、模型、Prompt 版本、调用类型、尝试次数、耗时、Token 统计、费用估算、闭集指标和错误短码。Key、Prompt、输入、原始输出、header、provider request ID 和异常文本不得写入 shell、数据库、报告或 Git。

真实模型结果具有非确定性，因此它是独立 opt-in 门禁；完整浏览器 E2E 使用确定性模型服务器，以保证可重放性。真实模型失败必须如实记录，不能通过修改期望或重复调用直到偶然成功来伪造通过。

## 7. Real-service E2E

完整 E2E 启动真实 PostgreSQL、Milvus、本地 BGE 模型、FastAPI、分析 Worker、优化/合规 Worker、人工修订 Worker、平台投递 Worker、确定性模型服务器和平台模拟器。浏览器仍使用 Chromium，但不再拦截管理或业务 API。

核心成功链路为：

1. 初始化确定性多店铺数据与知识版本。
2. operator 登录并在授权店铺发起分析。
3. Worker 完成分析，operator 选择商品并生成优化方案。
4. 合规与最多两次自动修订完成，operator 提交审批。
5. supervisor 登录并批准。
6. 本地发布事务创建平台投递，投递 Worker 成功调用模拟器。
7. 浏览器显示本地发布、平台投递终态、安全调用摘要和审计证据。

独立的失败链路覆盖：主管驳回、连续两次合规失败、平台提交后连接断开并幂等重放、Worker 中断后租约恢复、Milvus 不可用时的明确失败或降级，以及无效/重复 Webhook。

测试数据必须使用本轮唯一前缀并在结束时只清理本轮创建的 PostgreSQL、Milvus 和模拟器事实。不得删除共享卷、主数据库、用户文件或其他运行数据。

## 8. Fault-injection matrix

| Boundary | Injected fault | Required outcome |
|---|---|---|
| DeepSeek | timeout / 429 / invalid JSON | 有界尝试，安全错误码或一次 schema repair，无原始内容泄露 |
| Milvus | unavailable / timeout / zero hit | 明确 failed、degraded 或 zero-hit，不伪造引用 |
| Platform auth | expired token | 刷新一次并沿用同一投递幂等键 |
| Platform write | 429 / 5xx / timeout | 有界重试，状态和下次执行时间可观察 |
| Platform write | accepted then disconnect | 重放得到同一 external operation ID，只有一项远端副作用 |
| Worker | crash / lease expiry | 新 owner 恢复，旧 owner 不能提交终态 |
| Webhook | bad signature / stale timestamp | 拒绝且零业务写入 |
| Webhook | duplicate event ID | 幂等成功，只有一条 receipt 和一条接受审计 |
| Approval | repeated request | 一个批准事实、一个 PublishRecord、一个 PlatformDelivery |

## 9. UI and API evidence

现有发布与审批 API 保持兼容，只在安全响应中增加可空的平台投递摘要：状态、attempt count、external operation ID、安全错误码和完成时间。无投递任务的历史记录返回空摘要。

桌面和平板端在现有审批/任务详情中展示“本地发布”和“平台投递”两个事实。手机端继续只支持任务查看与主管审批，不增加系统管理或平台运维界面。浏览器不能修改 Token、base URL、重试次数或故障模式。

失败状态提供明确中文说明，但不显示内部异常。阶段十不增加手工重试按钮；临时故障由有界自动重试处理，终态失败通过安全审计和报告诊断。

## 10. Test and acceptance gates

阶段十完成必须同时通过：

1. 平台客户端、模拟器、投递状态机、租约、幂等和 Webhook 的离线单元测试。
2. PostgreSQL 上的 `0007` upgrade/downgrade/current/check、真实约束、事务原子性和并发领取测试。
3. 真实 Milvus 与本地模型的知识写入、检索、版本引用、停用和故障测试。
4. 三个 Agent 的显式真实 DeepSeek 契约 smoke；每个 Agent 的调用次数符合上限。
5. 不拦截真实业务 API 的完整 Playwright 成功链路和批准/故障链路。
6. 全量后端 pytest、前端 Vitest、TypeScript noEmit、生产构建、Playwright fixture 回归、compileall、Compose config 和 `git diff --check`。
7. 敏感信息扫描证明 Git diff、测试输出、报告和 API 响应不含配置的密钥或授权 header。
8. 测试结束后所有真实调用开关清除，隔离 worktree 干净，主检出和远端仅在单独批准后更新。

## 11. Final artifacts

阶段十只增加两份面向交付的文档：

- 根目录 `README.md`：项目定位、功能边界、架构、角色、快速启动、演示流程、测试命令、安全配置和诚实限制。
- `docs/phase10-validation-report.md`：提交、环境版本、受控真实调用摘要、各门禁命令与结果、故障注入矩阵、E2E 截图和已知非阻塞问题。

既有设计规格继续作为详细架构来源，不复制第二套架构文档。验证报告不得包含 Key、Token、连接凭据、Prompt、原始模型响应、服务器个人路径或未脱敏日志。

最终项目可以陈述：真实 DeepSeek 契约、真实本机 PostgreSQL/Milvus/Worker、完整真实服务浏览器链路和生产级平台投递故障语义已验证。它必须同时陈述：真实电商平台商家认证、正式 OAuth 和生产接口联调尚未执行。

## 12. Completion boundary

阶段十通过上述门禁并完成独立审查后，当前项目功能开发正式收尾。之后只有两类可选工作：

- 部署到用户选择的服务器或云环境。
- 获得真实商家与开放平台权限后，单独设计并实现某一个真实平台适配器、企业密钥托管和逐店 OAuth 授权。

两类工作都不属于当前第十阶段，不能在没有新增授权和设计的情况下自动开始。
