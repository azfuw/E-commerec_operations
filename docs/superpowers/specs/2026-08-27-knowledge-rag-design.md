# 阶段三：知识库与 Milvus 混合检索设计

> 状态：待用户书面复核的阶段三设计<br>
> 日期：2026-08-27<br>
> 项目路径：`D:\E-commerce_operations`
> 上游设计：[电商运营总设计](2026-08-25-ecommerce-operations-design.md)、[持久经营分析 Agent 设计](2026-08-26-durable-analysis-agent-design.md)

## 1. 目标与阶段终点

本阶段为本地电商运营演示系统提供可追溯的知识检索切片。管理员可导入、更新、停用版本化演示知识；独立知识 Worker 可恢复地解析、分块和索引文档；运营专员与运营主管可通过受服务端 RBAC 保护的检索调试 API 获得附带真实来源和版本的中文结果。

检索链路使用本地离线的 BGE-M3 生成 1024 维 dense 向量与 sparse 向量，在 Milvus 执行混合召回，再使用本地离线 `bge-reranker-v2-m3` 精排。PostgreSQL 始终是文档正文、版本和激活状态的事实源；Milvus 只是可重建的向量索引。固定的中文演示知识包和查询集用于校准和评测检索效果。

阶段通过的终点是：后台 API 能导入版本化演示知识，Worker 能可恢复地建立索引，检索能完成混合召回、精排、引用与故障降级。它只为下一阶段的商品优化预留可靠检索能力，不触发商品选择或商品优化。

## 2. 范围与明确排除

### 2.1 本阶段范围

- 支持 PDF、DOCX、Markdown 和 TXT 的本地知识导入。
- 系统管理员可创建文档、提交新版本、停用文档、查看文档及版本状态。
- 运营专员、运营主管和系统管理员可只读调用检索调试 API；只有系统管理员可修改知识资源。
- 使用当前数据库实时加载的用户角色执行授权；JWT 内的角色声明不能替代数据库中的当前用户状态和角色。
- 使用 PostgreSQL 版本行租约实现可恢复后台处理，使用 Milvus Standalone 实现 dense+sparse 混合召回，使用本地 reranker 完成精排。
- 提供确定性、平台中立、中文的演示规则包和固定查询评测集。

### 2.2 明确排除

- 商品选择、商品优化 Agent、合规 Agent、审批、模拟发布以及任何商品修改接口。
- 前端、真实电商平台接入、云部署、真实生产 SLA 和生产性能承诺。
- 新生成模型、模型 API Key、模型自动下载、联网模型服务或互联网检索。
- Redis、Celery、Kafka、MCP、微服务、额外 Compose Worker 服务和 Docker API 控制。
- Milvus 以外的向量数据库、RAG 云服务和知识图谱。
- 将演示规则描述为官方法规、官方平台规范或真实平台规则库。

## 3. 架构与运行边界

系统保持现有 FastAPI 模块化单体和 PostgreSQL。Milvus Standalone、etcd 与 MinIO 作为 Milvus 的必要基础设施加入既有 Docker Compose；etcd 与 MinIO 不构成项目业务对象存储，也不对外提供业务接口。

```text
系统管理员上传本地文件
        │
        ▼
FastAPI + PostgreSQL ──本地路径──► data/uploads/knowledge/...
        │ 202 accepted
        ▼
独立知识 Worker 命令（不作为 Compose 服务）
        │ PostgreSQL 租约 / 可恢复处理
        ├── 安全解析与确定性分块
        ├── 本地 BGE-M3（dense 1024 + sparse）
        ├── Milvus Standalone hybrid upsert
        └── PostgreSQL 激活版本与 chunk 元数据

运营/主管检索 API
        │ 实时 RBAC + active version IDs
        ├── 本地 BGE-M3 query dense+sparse
        ├── Milvus hybrid search
        ├── PostgreSQL canonical chunk 读取
        └── 本地 bge-reranker-v2-m3 精排 + 引用响应
```

本地模型未来从仓库根目录 `/model/` 离线路径加载：`model/bge-m3` 提供 dense+sparse 表征，`model/bge-reranker-v2-m3` 提供精排。实施的第一步必须将 `/model/` 加入 Git 忽略；本阶段不读取、移动、删除、暂存或提交该目录，也不修改 `.gitignore`。上传原文目录 `data/uploads/knowledge/` 同样应在未来实施时加入 Git 忽略。可提交的演示规则是小型、可审计的文本文件，而非模型权重或用户上传内容。

知识 Worker 以仓库内命令单独启动，例如未来的 `scripts/run_knowledge_worker.py --once`；它与分析 Worker 共用进程模型和 PostgreSQL 租约原则，但不改造 `analysis` workflow、`workflow_runs` 或现有分析 Worker。

## 4. 数据事实、状态与约束

### 4.1 PostgreSQL 事实表

后续迁移仅新增以下知识业务表；本阶段不实施迁移。

| 表 | 责任 | 关键列与约束 |
|---|---|---|
| `knowledge_documents` | 文档身份与当前激活版本 | `id`、`name`、`category`、`enabled`、`current_version_id`、`created_by`、`created_at`、`updated_at`。`current_version_id` 仅可指向同一文档的 `active` 版本；禁用文档的 current version 在查询中永不进入 active ID 集。 |
| `knowledge_document_versions` | 可恢复的导入版本、租约与处理审计 | `id`、`document_id`、单调 `version_number`、`sha256`、原始文件名、MIME、受控本地路径、`status`、`attempt_count`、`lease_owner`、`lease_expires_at`、解析器版本、分块版本、嵌入版本、`error_code`、时间戳。唯一约束为 `(document_id, version_number)` 与 `(document_id, sha256)`。 |
| `knowledge_chunks` | 规范正文与引用元数据 | `id`、`version_id`、`chunk_index`、稳定 `chunk_id`、`chunk_hash`、`canonical_text`、标题/段落元数据、`token_count`、时间戳。唯一约束为 `(version_id, chunk_index)`、`(version_id, chunk_hash)` 与全局 `chunk_id`。 |

`knowledge_document_versions.status` 只能为 `accepted`、`processing`、`active`、`failed`、`disabled`。`attempt_count` 范围为 0 到 3。只有 `processing` 版本可持有非空 `lease_owner` 与未来的 `lease_expires_at`；所有其他状态都必须清空两项。状态变更、续租、失败和激活均要求匹配当前 owner、`processing` 状态及未过期租约。`active` 和 `disabled` 是终态，不可重新领取。

`workflow_runs` 不被泛化为知识任务：现有数据库约束将其限定为 `analysis`，且要求分析日期和店铺输入。知识版本行自身承载租约与恢复状态，避免破坏已验证的经营分析闭环。

### 4.2 Milvus 索引记录

Milvus collection 只存可重建的检索字段：稳定字符串 `chunk_id` 主键、`document_id`、`version_id`、类别、1024 维 dense vector、BGE-M3 sparse vector 和最少的版本过滤元数据。规范正文、文件路径、处理错误、用户身份和 PostgreSQL 激活状态不以 Milvus 为权威。

`chunk_id` 由版本 SHA-256、`chunk_index` 和 `chunk_hash` 的稳定 UUID5 或等价稳定摘要派生。相同输入重试得到相同 ID；Milvus upsert 因而不会增加重复向量。chunk 的 canonical 文本和可展示引用始终从 PostgreSQL 按 `chunk_id` 读取。

## 5. 跨系统一致性与版本可见性

新版本按以下顺序处理：

1. Worker 在 PostgreSQL 领取版本租约，解析并形成稳定 chunks。
2. Worker 用稳定 `chunk_id` 将该版本全部 dense+sparse 向量幂等 upsert 到 Milvus。
3. Worker 写入或确认 PostgreSQL `knowledge_chunks` 元数据。
4. 仅在向量 upsert 与 chunk 元数据成功后，PostgreSQL 单一事务将新版本置为 `active`、更新 `knowledge_documents.current_version_id`，并把前一 active 版本置为 `disabled`。

因此查询始终先从 PostgreSQL 取得 `enabled=true` 文档的 active `version_id` 集，再把该集合施加为 Milvus 过滤条件。上载失败的新版本和任何孤儿向量都不在该集合内；旧版本在新版本成功激活前持续服务。重试复用同一 version 和 chunk ID，不会产生重复向量。

演示规模下，active-version ID filter 是清晰且正确的跨系统可见性边界。其容量边界是单次过滤集合和表达式长度随激活文档数线性增长；在未来需要远大于演示规模时，才评估按索引版本命名空间、分区或异步清理。该阶段不提前建设这些优化，也不为了清理孤儿向量阻塞激活或查询。

停用文档是 PostgreSQL 事务：立即设为 `enabled=false` 并从 active version 集排除。Milvus 向量可保留为不可见的可重建索引数据；未来异步清理不属于本阶段。正在处理的版本在后续 owner-guard 写入时发现文档已禁用，必须停止并保持不可见。

## 6. 安全导入与可恢复处理

### 6.1 上传边界

`POST /knowledge/documents` 与新版本上传只接受 `.pdf`、`.docx`、`.md`、`.txt`。服务端同时核对扩展名、声明 MIME 和文件魔数或格式结构；任一不匹配返回安全错误码。文件大小上限为 20 MiB，空文件被拒绝；PDF 页数上限为 200，解析后的规范文本上限为 1,000,000 个字符。

服务端生成受控路径 `data/uploads/knowledge/<document-id>/<version-id>/<server-generated-name>`，从不使用客户端文件名作为路径，也不接受客户端路径、`..`、绝对路径或符号链接跳转。DOCX 作为 ZIP 容器处理时限制条目数、单条与总解压大小，并拒绝损坏 ZIP、压缩炸弹、加密或不支持的嵌套内容。损坏 PDF、加密 PDF、解析超时和无法提取正文的文件均失败，不产出部分检索结果。

上传文件和已解析知识内容均是不可信数据：它们只能作为待索引文本，不能覆盖系统指令、改变 RBAC、触发工具调用、指定文件路径或改变 Worker 行为。日志、API 错误和审计记录不保存完整上传原文。

### 6.2 导入状态机

```text
accepted
  → processing
  → active
  → failed
  → disabled
```

- 安全上传后计算 SHA-256。相同 document 与 SHA-256 已存在时，返回既有 version 的安全状态，不创建新版本或新租约。
- `accepted` 版本可由 Worker 用 PostgreSQL `now()` 与 `FOR UPDATE SKIP LOCKED` 领取；过期 `processing` 版本可恢复领取。
- 每次领取增加 `attempt_count`；第三次领取失败后转 `failed/KNOWLEDGE_ATTEMPTS_EXHAUSTED`，不发生第四次领取。
- 处理步骤是解析、标题/段落感知分块、local BGE-M3 dense+sparse 嵌入、Milvus upsert、PostgreSQL chunk 元数据和原子激活。
- 每个可能阻塞的模型或 Milvus 操作前续租；失去租约的旧 Worker 不再发起后续外部依赖调用，也不写入、激活或覆盖新 owner 的状态。
- 进程中断时不伪造成功。未激活版本在租约到期后由下一 Worker 继续；稳定 ID 和 upsert 保证重放幂等。

### 6.3 确定性分块

解析器保留标题层级、页码、段落序号和来源顺序。先按标题切分，再按段落合并；超过 512 模型 token 的段落按句子边界拆分，使用 64 token 重叠。无法按句子边界拆分的超长文本按 Unicode 字符边界稳定拆分。空白标准化、标题路径和段落索引进入元数据；同一文件版本的处理必须产生相同 chunk 顺序、正文和 `chunk_id`。

## 7. 混合检索、精排与引用

### 7.1 查询流程

1. 服务端验证当前数据库用户的只读知识权限、查询长度和可选类别过滤。
2. 从 PostgreSQL 获取启用文档的 active `version_id` 集；集合为空时返回 `zero_hit`，不查询 Milvus。
3. 本地 BGE-M3 为查询生成 dense 与 sparse 表征。
4. Milvus 在 active version filter 和可选类别过滤内执行 hybrid search，返回候选 `chunk_id` 与召回阶段分数。
5. PostgreSQL 读取候选的 canonical chunks，拒绝任何不属于 active version 的返回项。
6. 本地 `bge-reranker-v2-m3` 对查询与 canonical 文本精排，产生最终分数。
7. 应用由固定演示查询集校准出的阈值路由，返回 `normal`、`low_confidence` 或 `zero_hit` 质量状态及可追溯引用。

融合策略、dense/sparse 候选数量、最终 top-k 与置信度阈值必须由固定评测集校准并记录使用的检索配置版本；不得复制未验证的经验权重或经验常量。校准前的实现不能把任意权重声称为已验证最佳策略。

### 7.2 响应与引用

每个命中包含 canonical 内容、文档名、文档版本、`chunk_id`、类别、dense 阶段分数、sparse 阶段分数、融合分数、reranker 分数和最终分数。内容来自 PostgreSQL canonical chunk，不来自 Milvus 的非权威副本。响应不包含服务器文件路径、上传用户身份、隐藏模型指令、向量本体或完整内部错误。

`zero_hit` 表示 active 知识中无可用命中；`low_confidence` 表示有命中但未达校准阈值。两者都不允许伪造检索结果、默认法规文本或模型生成答案。

## 8. API 契约与授权

所有接口返回统一 envelope：

```text
{
  request_id,
  status,
  data,
  quality,
  error
}
```

`error` 仅包含稳定安全错误码和可用户理解的短消息；不会包含栈、路径、密钥、原文、完整查询 Prompt 或底层依赖响应。分页响应含 `items`、`page`、`page_size`、`total`；默认 `page_size=20`，最大 100。检索 `top_k` 默认 10，最大 20，查询正文长度为 1 到 500 个 Unicode 字符。

| 接口 | 授权与请求 | 成功响应 | 幂等与主要错误 |
|---|---|---|---|
| `POST /knowledge/documents` | 仅当前启用的系统管理员；multipart 文件、`name`、`category` 与可选客户端幂等键 | `202`，`data={document_id, version_id, status:"accepted"}` | 相同 document SHA 返回已有安全 version，不重复入队；类型、MIME、大小、空文件、损坏内容分别返回安全 4xx。 |
| `GET /knowledge/documents` | 仅当前启用的系统管理员；可按类别、启停和版本状态筛选 | `200`，分页文档和安全版本状态 | 不返回本地路径、文件原文或 Worker owner。 |
| `POST /knowledge/documents/{id}/versions` | 仅当前启用的系统管理员；multipart 新文件及可选幂等键 | 新 SHA 为 `202 accepted`；相同 SHA 为 `200` 已有 version 状态 | 未知文档为 404；已停用文档为 409；不替换旧 active version 直到新版本激活。 |
| `POST /knowledge/documents/{id}/disable` | 仅当前启用的系统管理员 | `200`，`data={document_id, enabled:false}` | 重复停用保持 200；事务后立即从 active version 集移除。 |
| `POST /knowledge/search` | 当前启用的运营专员、运营主管或系统管理员；JSON `query`、可选 `categories`、可选 `top_k` | `200`，排序结果、来源、版本、chunk ID、阶段/最终分数及质量状态 | 是只读检索调试接口；无权限 403，`zero_hit` 与 `low_confidence` 为稳定业务质量状态，不伪造结果。 |

身份检查必须先于资源存在性披露。管理员修改接口和检索接口都重新从数据库加载当前用户；禁用用户统一得到认证失败。知识文档是全局演示资源，不按店铺隔离，因此本阶段不引入店铺参数或跨店铺知识范围抽象。

主要安全错误码包括 `KNOWLEDGE_FILE_TYPE_INVALID`、`KNOWLEDGE_MIME_MISMATCH`、`KNOWLEDGE_FILE_TOO_LARGE`、`KNOWLEDGE_FILE_EMPTY`、`KNOWLEDGE_FILE_CORRUPT`、`KNOWLEDGE_PATH_INVALID`、`KNOWLEDGE_DOCUMENT_NOT_FOUND`、`KNOWLEDGE_DOCUMENT_DISABLED`、`KNOWLEDGE_PARSE_FAILED`、`KNOWLEDGE_MODEL_UNAVAILABLE`、`KNOWLEDGE_MILVUS_UNAVAILABLE`、`KNOWLEDGE_LEASE_LOST`、`KNOWLEDGE_ATTEMPTS_EXHAUSTED` 与 `KNOWLEDGE_DEPENDENCY_TIMEOUT`。

## 9. 故障、恢复与质量路由

| 情况 | 外部可见结果 | 后台状态与规则 |
|---|---|---|
| 无 active version 或无候选 | `200`、`quality.status=zero_hit`、空结果 | 不伪造命中。 |
| 候选未达校准阈值 | `200`、`quality.status=low_confidence`、返回低置信真实引用 | 不将其提升为正常结果。 |
| BGE-M3、reranker 或 Milvus 不可用 | `503`、`dependency_error` | 版本处理保留或转安全失败；查询不返回虚构结果。 |
| 依赖超时 | `503`、`KNOWLEDGE_DEPENDENCY_TIMEOUT` | 未完成版本可由租约恢复；不把超时标为 active。 |
| PDF/DOCX 解析失败 | 导入状态可查询为 `failed` | `error_code=KNOWLEDGE_PARSE_FAILED`，旧 active 版本继续服务。 |
| Worker 中断或失租 | API 保留已有安全状态 | 旧 owner 停止，租约过期后恢复领取；不覆盖新 owner。 |
| 新版本 Milvus upsert 或 DB 激活失败 | 新版本 `failed` 或可恢复 `processing` | 当前 active 版本不变，孤儿向量不可见。 |
| 文档被停用 | 随后的检索立即不含其结果 | PostgreSQL active ID 集移除该文档，Milvus 残留向量不可见。 |

处理和查询不得以通用知识、缓存的旧结果或生成式文本伪造结果。解析、嵌入、Milvus、rerank 和数据库阶段均记录安全阶段代码、耗时、版本信息和请求 ID，但不记录完整文档正文、查询 Prompt、模型权重路径细节或凭据。

## 10. 平台中立演示知识包

演示包是小型可提交中文文本集合，每份文件和每条检索引用都标注“项目演示规则：平台中立、非官方法规或平台规范”。它覆盖以下规则主题：

- 标题关键词：核心品类词、规格词和避免堆砌的演示写法。
- 卖点与详情：事实可追溯、避免凭空承诺、结构化表达。
- 禁限词与夸大宣传：绝对化、医疗化、无法证明的效果和诱导性表述示例。
- 类目属性：类目、材质、尺寸、适用场景等字段应与已有商品事实一致。
- SKU、价格与规格一致性：标题、详情、SKU 规格、价格和库存描述不得互相冲突。
- 售后与退款：退换、退款、质保等表述应清晰、非误导且与展示信息一致。

这些规则只用于演示检索、固定评测和后续本地优化建议的可引用依据；它们不取代法律意见、平台政策或人工审核。

## 11. 测试、评测与验收

普通测试完全 mock Milvus、BGE-M3 和 reranker，不加载本地权重、不联网。重点覆盖：

- 文件扩展名/MIME/大小/空文件/路径穿越/损坏 PDF 与 DOCX ZIP 边界。
- SHA 幂等导入、同文档版本切换、停用立即不可见、旧版本服务连续性。
- `FOR UPDATE SKIP LOCKED` 互斥领取、过期恢复、三次上限、失租 owner 保护和中断恢复。
- 稳定 chunk ID、Milvus 幂等 upsert、孤儿向量不可见、重复处理不产生重复 chunk 或向量。
- BGE-M3 或 Milvus 缺失、超时、解析失败和 reranker 不可用时的安全错误与无伪造结果。
- 服务端 RBAC：只有管理员修改；运营专员与主管只能读检索；禁用用户和无角色用户被拒绝。

真实 PostgreSQL、Milvus Standalone 与本地离线模型的集成测试只在显式 opt-in 环境开关下运行。它使用固定演示知识包，清理只删除自己创建的 document/version/chunk 和对应稳定向量 ID；不删除用户知识、模型目录、数据库或 Milvus collection 的无关数据。

固定中文查询集对 dense-only、sparse-only、hybrid 和 hybrid+rereanker 四个路径进行比较。验收目标为：

| 指标 | 演示集目标 |
|---|---:|
| Recall@10 | ≥ 90% |
| 引用文档与版本正确率 | 100% |
| 重复向量 | 0 |
| MRR | 记录实际值，不预设生产承诺 |
| 查询与索引延迟 | 记录实际值，不预设生产 SLA |

评测报告必须写明数据集版本、模型版本、检索配置版本和运行环境。演示集结果只说明本地固定数据上的验证，不冒充生产指标。

## 12. 安全、可观测性与简历表述边界

- API 和 Worker 不读取或输出任何生成模型 API Key；本阶段检索模型仅从本地离线路径加载。
- 上传原文、原始模型输入、内部 reranker 文本、完整向量、Authorization、Cookie、密码和密钥不进入日志、错误响应、审计公开字段或评测展示。
- 请求审计记录 actor、资源 ID、文档/version 状态迁移、请求 ID、安全错误码和耗时；它不记录完整知识正文。
- 文件路径只在受控服务端内部使用，且不返回客户端。
- 本项目是本地演示系统，演示规则是模拟的、平台中立规则；无真实平台接入、云部署、官方规则库或生产 SLA。
- 简历和对外说明只能陈述已实际实现并验证的本地能力；本规格的设计内容在实施和验证前不得写成已完成能力。

## 13. 阶段完成边界

本阶段完成后，系统只具备版本化演示知识的导入、可恢复索引、Milvus hybrid retrieval、local rerank、真实来源与版本引用、固定集评测以及清晰故障状态。它不创建商品选择、商品优化、合规审批、模拟发布、前端或真实平台行为，也不修改已完成的经营分析 workflow 和 Worker。
