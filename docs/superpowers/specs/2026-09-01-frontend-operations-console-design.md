# 运营管理台前端设计

> 状态：已批准
> 日期：2026-09-01
> 项目路径：`D:\E-commerce_operations`
> 上游：[总设计](2026-08-25-ecommerce-operations-design.md)、[商品优化与合规设计](2026-08-28-product-optimization-compliance-design.md)、[人工修订、审批与模拟发布设计](2026-08-31-manual-review-approval-publish-design.md)

## 1. 目标与边界

### 1.1 阶段目标

本阶段实现总设计实施顺序第 8 阶段的 Vue 运营管理台，使三个现有角色能够通过浏览器完成下列本地业务闭环：

```text
登录
-> 从任务工作台恢复业务任务
-> 发起经营分析并查看候选商品
-> 选择商品进入优化
-> 查看建议、合规结果并人工修订
-> 提交审批
-> 主管或管理员批准、驳回或要求修改
-> 查看本地模拟发布结果
```

首页固定采用“任务工作台优先”，不改为传统 BI 指标首页。前端必须准确展示真实服务端状态，不得靠浏览器保存 workflow 或 proposal ID 伪造可恢复任务列表。

### 1.2 本阶段不做

- 不实现知识库、Agent 评测、全量审计日志或系统管理页面；这些属于总设计第 9 阶段。
- 不实现真实电商平台、真实发布、价格或 SKU 修改、价格专员角色和价格审批流程。
- 不实现经营大屏、复杂图表、暗色主题、离线缓存、Service Worker 或“记住登录”。
- 不引入 Pinia、Axios、Tailwind、图表库、OpenAPI 代码生成器、Nginx 或独立前端容器。
- 不重写现有认证、审批、模拟发布、Worker、Agent、RAG 或数据库模型。
- 不在本阶段声称完成真实 Worker、PostgreSQL 和故障注入的完整浏览器 E2E；该验收属于总设计第 10 阶段。

## 2. 已批准的设计决策

1. 技术栈采用 Vue 3、TypeScript、Vite、Vue Router、Element Plus 与 Element Plus 官方图标。
2. 前端位于仓库 `frontend/`；开发时由 Vite 代理 FastAPI，生产构建由 FastAPI 在 `/app` 下同源提供。
3. 继续使用现有 Bearer JWT。Token 只保存在 `sessionStorage`，页面启动时通过 `GET /auth/me` 恢复身份。
4. 新增 `GET /auth/me` 和 `GET /workbench/tasks` 两个最小只读接口，不增加 proposal 列表接口。
5. `/workbench` 与 `/proposals` 共用工作台读模型；后者只筛选 proposal 类任务。
6. 前端权限仅控制展示、路由和交互。服务端角色、店铺授权、资源归属与业务状态校验仍是最终安全边界。
7. `operator` 可发起分析、选品、修订和提交，不能审批；`supervisor` 与 `admin` 可修订、提交并审批自己或他人的方案。
8. 所有角色只能查看价格与 SKU 建议，不能在本阶段修改或发布应用这些建议。
9. 桌面端提供完整功能，平板端提供完整功能并重排布局，手机端只允许查看任务和由主管或管理员执行审批。
10. 前端浏览器链路使用确定性 API fixture；真实全栈浏览器 E2E 留给第 10 阶段。

## 3. 视觉与交互方向

### 3.1 设计判断

这是面向国内电商运营专员、主管和管理员的数据密集型内部管理台，不是营销页、品牌站或实验性作品集。

- `DESIGN_VARIANCE: 3`
- `MOTION_INTENSITY: 2`
- `VISUAL_DENSITY: 8`
- 设计系统：Element Plus，禁止混入第二套组件系统。
- 页面主题：固定中性浅色主题。本阶段不增加暗色模式。

### 3.2 视觉规则

- 页面背景使用冷中性浅灰，主内容面为白色，正文使用深灰。
- 唯一品牌强调色使用克制的蓝绿色；成功、警告和错误色仅表达真实语义状态。
- 中文字体优先使用系统字体，不下载 Web Font。数字使用 `font-variant-numeric: tabular-nums`。
- 输入、按钮、面板统一使用紧凑的 6px 圆角；状态标签可以使用胶囊形，但只能承载状态。
- 表格、分栏详情、时间线和稀疏分隔线优先。只有真实层级需要时才使用卡片和阴影。
- 不使用渐变标题、玻璃拟态、装饰发光、滚动劫持、自动轮播或无业务含义的动画。
- 动效仅用于抽屉、弹窗、状态切换和按键反馈，并遵守 `prefers-reduced-motion`。
- 所有按钮、输入、文本和状态达到 WCAG AA 对比度；颜色不是状态的唯一表达方式。

## 4. 信息架构与路由

| 路由 | 页面职责 | 桌面/平板 | 手机 |
|---|---|---:|---:|
| `/app/login` | 登录 | 完整 | 完整 |
| `/app/workbench` | 待处理、运行中、异常和最近任务 | 完整 | 只读 |
| `/app/analysis` | 发起分析 | 完整 | 不开放 |
| `/app/analysis/:runId` | 分析状态、候选商品和选品 | 完整 | 只读 |
| `/app/proposals` | 优化方案任务列表 | 完整 | 只读 |
| `/app/proposals/:id` | 差异、合规、修订、提交和发布结果 | 完整 | 只读；主管/管理员可审批 |
| `/app/approvals` | 待审批列表 | 主管/管理员 | 主管/管理员 |

侧边导航固定为“工作台、经营分析、优化任务、审批中心”。审批中心对 `operator` 隐藏。第 9 阶段页面不显示不可用占位导航。

未登录用户访问受保护路由时进入登录页。登录后默认进入 `/app/workbench`。已登录用户访问登录页时返回工作台。无权限路由显示明确的 403 页面，不伪装成空列表。

## 5. 响应式布局

### 5.1 桌面

- 左侧固定导航，右侧主工作区。
- 表格保留关键业务列，低优先级详情通过抽屉展开。
- 方案差异使用左右对比，审批动作位于详情头部或右侧操作区。

### 5.2 平板

- 保留所有操作能力。
- 侧栏折叠，左右差异改为上下排列，筛选器允许换行。
- 表格低优先级列隐藏，详情通过行展开或抽屉查看。

### 5.3 手机

- 工作台、分析详情和方案详情只读。
- 隐藏发起分析、选择商品、人工修订和提交审批入口。
- `supervisor` 与 `admin` 可进入审批列表和方案摘要，并使用固定底部操作区批准、驳回或要求修改。
- `operator` 不能看到审批入口或审批按钮。
- 所有复杂表格改为按任务排列的摘要行，避免横向滚动作为主要交互。

前端使用路由 meta 和同一个权限函数控制手机与角色入口；服务端仍对每次请求重新鉴权。

## 6. 前端技术架构

### 6.1 最小模块边界

```text
frontend/
├── package.json / package-lock.json
├── vite.config.ts / tsconfig*.json / index.html
├── src/
│   ├── main.ts / App.vue
│   ├── router.ts
│   ├── api.ts
│   ├── session.ts
│   ├── types.ts
│   ├── styles.css
│   ├── pages/
│   └── components/
├── tests/
└── playwright.config.ts
```

- `session.ts` 是一个 Vue `reactive` 单例，只保存 Token、当前用户和会话恢复状态。
- `api.ts` 是唯一网络边界，负责 Bearer Header、`cache: "no-store"`、JSON 解析、错误归一化、AbortSignal 和幂等键。
- `types.ts` 只定义页面实际消费的接口，不复制未使用的后端 Schema。
- 页面保有自己的筛选、表单和轮询状态；没有跨页面需求的状态不进入全局 session。
- 通用组件只提取确有两个以上调用方的状态标签、错误面板、任务时间线和证据抽屉，禁止为单一页面预建抽象层。

### 6.2 认证会话

1. 登录成功后把 `access_token` 写入 `sessionStorage`。
2. 应用启动时若存在 Token，调用 `GET /auth/me`。
3. `/auth/me` 成功后写入当前用户；失败为 401 时清除整个会话。
4. 所有受保护请求附带 `Authorization: Bearer <token>`。
5. 退出登录清除 Token、用户对象和页面内缓存，并返回登录页。

前端不把 JWT payload 当作最终权限事实，也不把密码、Token、完整方案或审批意见写入控制台。

### 6.3 轮询与写操作

- 长任务采用串行递归轮询：一次请求完成后才通过 `setTimeout` 安排下一次。
- 默认间隔 2 秒；连续两次网络失败后改为 5 秒。成功响应恢复 2 秒。
- 进入终态、路由离开、组件卸载或用户退出时取消定时器和当前请求。
- 写操作使用 `crypto.randomUUID()` 生成 `Idempotency-Key`，同一次页面内重试复用原键。
- 请求期间禁用重复点击。结果不确定时先刷新服务端事实；状态仍允许且原键仍在内存中时才显示“重试”。
- 页面刷新后不持久化审批正文或写请求体。刷新后的服务端状态决定是否仍可操作。

## 7. 最小后端只读支持

本阶段不新增数据库表或迁移。

### 7.1 当前用户

```http
GET /auth/me
Authorization: Bearer <token>
```

```json
{
  "id": "user-id",
  "username": "operator01",
  "role": "operator"
}
```

接口复用现有 `get_current_user`，被禁用、删除或 Token 失效的用户返回 401。

### 7.2 工作台任务

```http
GET /workbench/tasks?page=1&page_size=20&store_id=<optional>&kind=<optional>&status=<optional>
Authorization: Bearer <token>
```

查询参数：

- `page`：从 1 开始。
- `page_size`：1-100，默认 20。
- `store_id`：可选，只能是当前用户有权访问的店铺。
- `kind`：可选，`analysis | proposal`。
- `status`：可选，使用现有 `WorkflowStatus`。

响应：

```json
{
  "items": [
    {
      "id": "business-task-id",
      "kind": "proposal",
      "store_id": "store-id",
      "product_id": "product-id",
      "analysis_run_id": "analysis-run-id",
      "proposal_id": "proposal-id",
      "workflow_run_id": "current-workflow-id",
      "workflow_type": "optimization",
      "status": "pending_approval",
      "quality_status": "normal",
      "current_step": "approval_pending",
      "action_required": "review_approval",
      "requires_current_user_action": true,
      "created_by": "user-id",
      "updated_at": "2026-09-01T10:00:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

`action_required` 固定为：

- `wait`
- `select_product`
- `edit_proposal`
- `submit_proposal`
- `review_approval`
- `view_result`
- `resolve_failure`

任务归并规则：

1. 尚未生成 proposal 的 analysis run 产生一个 `kind=analysis` 任务，`id` 使用 analysis run ID。
2. analysis run 已生成 proposal 后，不再单独返回该 analysis 任务；每个 proposal 只产生一个 `kind=proposal` 任务，`id` 使用 proposal ID。
3. proposal 有活动 manual review 时，`workflow_run_id` 指向该 manual workflow；否则指向原 optimization workflow。
4. `requires_current_user_action` 与 `action_required` 由服务端根据当前状态和当前角色计算，但不取代写接口的实时鉴权。
5. 默认排序为需要当前用户操作优先，其次 `updated_at DESC`，最后 `id ASC`，确保分页稳定。
6. 查询复用现有店铺访问语义，不扩大任何角色的店铺范围。未知或无权访问的 `store_id` 不返回跨店信息。

`/proposals` 页面调用 `kind=proposal` 的同一接口，不新增重复列表端点。

## 8. 页面行为

### 8.1 登录

- 用户名和密码均有可见标签。
- 提交期间禁用按钮；错误显示在表单内。
- 不区分用户名不存在、密码错误或用户已停用。

### 8.2 工作台

- 顶部以紧凑数字区显示“待我处理、运行中、异常、最近完成”。
- 主表显示店铺、商品、当前阶段、质量状态、下一动作和更新时间。
- 支持店铺、任务类型与状态筛选；筛选无结果与系统无任务使用不同空状态。
- “继续处理”按服务端关联 ID 进入分析或方案详情。
- 运行中任务进入详情后轮询，不在列表页为每一行创建独立轮询器。

### 8.3 经营分析

- 表单从 `GET /stores` 读取授权店铺，日期范围限制为 1-90 天。
- `POST /analysis-runs` 返回 workflow ID 后进入 `/app/analysis/:runId`。
- 详情轮询 `GET /workflow-runs/{id}`；进入 `awaiting_selection` 后读取候选商品。
- 候选表展示排名、异常类型、业务影响、核心指标、置信度和建议动作。
- 证据在抽屉内以纯文本展示。
- 选品前二次确认，成功后使用返回的 proposal ID 进入方案详情。
- 首版不引入图表库；指标使用数字、表格和轻量 CSS 趋势表达。

### 8.4 方案详情与人工修订

- 详情来源为 `GET /proposals/{id}`。
- 顶部根据当前服务端事实显示五阶段时间线：经营分析、运营选品、AI 优化、主管审批、模拟发布。
- 内容区展示当前 revision、合规结果、审批动作和发布结果。
- 桌面左右对比，平板上下对比，手机只读摘要。
- 可编辑组仅为标题、卖点、结构化详情、关键词和属性补全。
- 引用、证据、价格建议与 SKU 建议只读；客户端不得提交 citations、price suggestions 或 SKU suggestions。
- 前端根据父 revision 生成允许的结构化 `changes`，属性补全独立提交；服务端继续执行完整可信事实和引用验证。
- 只有 `draft_ready` 与 `pending_manual` 显示人工修订入口。
- 人工 revision 创建后轮询新 manual workflow，直到回到 `draft_ready`、`pending_manual` 或 `failed`。
- 只有通过复核的 `draft_ready` revision 可提交审批。

### 8.5 审批与模拟发布

- `/approvals` 使用现有分页 API，仅 `supervisor` 和 `admin` 可访问。
- 审批详情复用方案详情，不另建重复详情模型。
- 批准需要确认弹窗；驳回和要求修改必须输入 1-500 字意见。
- `supervisor` 与 `admin` 允许审批自己提交的方案；`operator` 没有审批入口。
- 批准成功后重新加载详情并显示 publish record、发布前后快照和商品版本变化。
- 页面必须明确说明这是本地模拟发布，价格、SKU、库存与真实平台均未变更。

## 9. 状态、错误和安全呈现

### 9.1 状态

- 所有后端枚举通过一个集中映射表转换为中文标签、语义颜色和下一动作。
- `partial`、`degraded` 和 `failed` 显示真实质量或失败含义，不能使用成功样式。
- 不可操作按钮保留在上下文中并显示禁用原因，避免用户误认为功能丢失。
- 自动状态更新使用 `aria-live="polite"`，不得抢夺键盘焦点。

### 9.2 错误

| HTTP | 前端行为 |
|---|---|
| 401 | 清除会话并进入登录页 |
| 403 | 显示无权限状态，保留当前路由上下文 |
| 404 | 显示资源不存在或已无访问权限 |
| 409 | 刷新服务端详情后显示状态、版本或重复操作冲突 |
| 422 | 映射到具体输入字段；无法映射时显示表单摘要 |
| 5xx/未知 | 显示通用提示和安全请求标识，不显示堆栈 |

列表加载失败时保留筛选条件。详情局部失败不清空已经成功加载的区域。错误解析必须兼容当前 API 的字符串 `detail`、对象 `detail.code` 和知识接口 envelope，不假设所有旧接口已统一响应格式。

### 9.3 安全与可访问性

- 服务端返回的标题、详情、证据、审批意见和引用全部使用 Vue 文本绑定，不使用 `v-html`。
- 所有受保护 GET 使用 `cache: "no-store"`。
- 表单使用可见 label，placeholder 不替代标签。
- 键盘可完成登录、筛选、修订和审批；弹窗关闭后焦点返回触发按钮。
- 状态使用图标或文字配合颜色，不能只有颜色。
- 前端长度校验与后端一致，但任何前端校验都不替代服务端边界。

## 10. 静态托管与本地运行

- Vite 开发服务器代理现有 API 根路径到 FastAPI，不修改现有 API URL。
- `npm run build` 输出 `frontend/dist`。
- FastAPI 在 `/app` 提供构建后的 `index.html` 与 assets，并对 `/app/*` 页面路由返回 SPA 入口。
- API 和健康检查路由必须先注册且保持原路径，SPA fallback 只能处理 `/app` 前缀。
- `frontend/dist`、`node_modules`、Playwright 浏览器、测试报告和覆盖率文件不进入 Git。
- npm cache 和 Playwright browser path 固定在 `D:\E-commerce_operations_env`；实施命令不得向 C 盘创建项目缓存。
- 现有 Compose 本阶段继续只管理 PostgreSQL、Milvus、etcd 与 MinIO；不新增独立前端或 Nginx 容器。

## 11. 测试与验收

本机规划时已确认 Node.js `24.15.0` 与 npm `11.12.1` 可用。

### 11.1 后端 API

pytest 覆盖：

- `/auth/me` 成功、Token 失效、用户禁用。
- 工作台角色与店铺范围。
- analysis/proposal 归并和每个 proposal 单行规则。
- 活动 manual workflow 选择。
- action required 角色映射。
- 分页、筛选和稳定排序。
- 未知或越权店铺筛选不泄漏数据。

### 11.2 前端组件

Vitest 与 Vue Test Utils 覆盖：

- 登录和会话恢复。
- 工作台 loading、empty、filtered empty、error、disabled 与 replay。
- 串行轮询、终态停止和卸载取消。
- 候选商品展示、证据抽屉和选品确认。
- 五个可编辑组、只读引用、只读价格与只读 SKU。
- operator 禁止审批，supervisor/admin 自审。
- 401、403、409、422 和未知错误映射。

### 11.3 浏览器链路

Playwright 使用确定性 API fixture 覆盖：

1. 运营登录、发起分析、等待候选、选择商品。
2. 查看方案、人工修订、等待复核、提交审批。
3. 主管登录、自审批准、查看模拟发布记录。
4. 主管要求修改后再次修订和提交。
5. 手机端隐藏创建、选品、修订和提交入口，同时保留查看与主管审批。

该测试只称为“前端浏览器链路”。第 10 阶段另行运行真实 FastAPI、PostgreSQL、Worker、模型 mock、故障注入和完整浏览器 E2E。

### 11.4 必须通过的门禁

```powershell
npm run typecheck
npm run test
npm run test:e2e
npm run build
D:\E-commerce_operations_env\python.exe -m pytest
git diff --check
```

另有一个后端 smoke 验证构建后 `/app`、嵌套路由 fallback 和静态 asset 可读取，且现有 API 与 `/health/live` 不被 SPA fallback 捕获。

## 12. 实施与审查边界

实施计划按以下七个独立审查单元拆分：

1. 当前用户与工作台只读 API。
2. Vue 工程、认证与应用外壳。
3. 任务工作台。
4. 经营分析与候选商品。
5. 方案差异、合规结果与人工修订。
6. 审批、模拟发布结果与移动端限制。
7. 浏览器链路、生产构建与完整回归。

每个任务必须执行 RED、GREEN、相关回归、精确暂存白名单、提交和审查窗口放行。规划窗口只编写并审核规格和计划；代码、依赖安装与测试执行仍由“电商项目开发窗口”在隔离 worktree 中完成。

## 13. 最终验收标准

1. 三种角色可以登录并恢复真实身份，失效会话安全退出。
2. 工作台跨刷新恢复授权任务，不依赖浏览器保存业务 ID。
3. 桌面端完成分析、选品、修订、提交、审批和查看模拟发布结果。
4. 平板端完成同一业务闭环，布局无关键内容遮挡。
5. 手机端只能查看任务和由主管或管理员审批，不能创建或修改业务内容。
6. operator 不能审批，supervisor/admin 可以自审，服务端权限测试通过。
7. 价格与 SKU 始终只读，模拟发布不改变价格、SKU 或库存事实。
8. loading、empty、error、disabled、degraded 和 replay 状态均有可验证界面。
9. 类型检查、组件测试、前端浏览器链路、生产构建、静态托管 smoke 和完整后端回归全部通过。
10. 文档只声明本地模拟发布和前端 fixture 浏览器链路，不夸大为真实平台或完整全栈 E2E。
