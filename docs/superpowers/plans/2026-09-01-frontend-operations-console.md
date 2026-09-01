# 运营管理台前端实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 FastAPI 业务闭环上交付可恢复的 Vue 3 运营管理台，覆盖经营分析、选品、方案修订、审批和本地模拟发布结果。

**Architecture:** 前端是位于 `frontend/` 的单个 Vue SPA，开发时通过 Vite 代理现有 API，生产构建由 FastAPI 在 `/app` 同源提供。后端只增加当前用户和统一工作台两个只读能力；浏览器继续使用现有 Bearer JWT，所有写操作继续走现有服务端状态机和幂等边界。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy asyncio、Vue 3.5.42、TypeScript 7.0.2、Vite 8.2.2、Vue Router 5.3.0、Element Plus 2.14.5、Vitest 4.1.11、Vue Test Utils 2.5.0、Playwright 1.62.1

**Spec:** `docs/superpowers/specs/2026-09-01-frontend-operations-console-design.md`

## Global Constraints

- 实施必须在“电商项目开发窗口”的隔离 worktree 中进行；审查窗口不直接实现功能。
- 桌面端完整、平板端完整、手机端只读任务和主管/管理员审批。
- 前端使用 Vue 3、TypeScript、Vite、Vue Router、Element Plus 和 Element Plus 官方图标。
- 不引入 Pinia、Axios、Tailwind、图表库、OpenAPI 代码生成器、Nginx 或独立前端容器。
- Token 只存 `sessionStorage`；不实现离线缓存、Service Worker、“记住登录”或暗色主题。
- 前端权限不是安全边界；所有角色、店铺、资源归属和状态仍由现有 FastAPI 服务校验。
- 人工可编辑标题、卖点、结构化详情、关键词和有可信证据的属性补全；引用、证据、价格和 SKU 只读。
- 本地模拟发布不得改变价格、SKU、库存或调用真实平台。
- 浏览器测试只声明为确定性 fixture 驱动的前端链路；真实全栈 E2E 留给总设计第 10 阶段。
- npm cache 使用 `D:\E-commerce_operations_env\npm-cache`；Playwright 浏览器使用 `D:\E-commerce_operations_env\playwright-browsers`；不得向 C 盘写项目缓存。
- Node.js 基线为本机已验证的 `24.15.0`，npm 基线为 `11.12.1`。
- `frontend/dist`、`frontend/node_modules`、coverage、Playwright report 和 test-results 不提交。
- 每个任务只暂存列出的精确文件，运行 `git diff --cached --check` 后提交，并停止等待审查窗口放行。

---

## File Map

### 后端

- `backend/workbench.py`：工作台只读归并、角色动作计算、稳定排序和分页。
- `backend/schemas.py`：`CurrentUserView`、`WorkbenchTaskView`、`WorkbenchTaskListView`。
- `backend/routes.py`：`GET /auth/me`、`GET /workbench/tasks`。
- `backend/main.py`：只在构建目录存在时提供 `/app` SPA 和 assets。
- `tests/test_workbench_api.py`：工作台读模型与 API 权限测试。
- `tests/test_auth_and_scope.py`：当前用户接口测试。
- `tests/test_frontend_hosting.py`：静态资源和 SPA fallback 测试。

### 前端基础

- `frontend/package.json`、`frontend/package-lock.json`：固定依赖和命令。
- `frontend/index.html`、`frontend/vite.config.ts`、`frontend/tsconfig.json`、`frontend/tsconfig.app.json`：Vite 与严格 TypeScript 配置。
- `frontend/src/main.ts`、`frontend/src/App.vue`、`frontend/src/router.ts`：启动、路由和应用根。
- `frontend/src/api.ts`：唯一 HTTP 边界和错误归一化。
- `frontend/src/session.ts`：会话状态和 `sessionStorage`。
- `frontend/src/types.ts`：前端实际消费的 API 类型。
- `frontend/src/capabilities.ts`：角色、viewport 和业务状态的展示能力判断。
- `frontend/src/status.ts`：后端枚举的唯一中文状态映射。
- `frontend/src/useSerialPoll.ts`：分析和人工复核共用的串行轮询。
- `frontend/src/styles.css`：Element Plus token、浅色主题、密度和响应式规则。

### 页面与组件

- `frontend/src/pages/LoginPage.vue`：登录。
- `frontend/src/pages/ForbiddenPage.vue`：明确的无权限状态。
- `frontend/src/pages/WorkbenchPage.vue`：工作台和 proposal 筛选列表。
- `frontend/src/pages/AnalysisPage.vue`：发起分析。
- `frontend/src/pages/AnalysisRunPage.vue`：轮询、候选和选品。
- `frontend/src/pages/ProposalPage.vue`：方案详情、修订、提交和审批结果。
- `frontend/src/pages/ApprovalsPage.vue`：待审批列表。
- `frontend/src/components/AppShell.vue`：导航和退出。
- `frontend/src/components/StatusTag.vue`：集中状态映射展示。
- `frontend/src/components/InlineError.vue`：安全错误与重试。
- `frontend/src/components/EvidenceDrawer.vue`：纯文本证据。
- `frontend/src/components/ProposalDiff.vue`：原值与建议值对比。
- `frontend/src/components/CompliancePanel.vue`：合规结果与修改要求。
- `frontend/src/components/ManualRevisionForm.vue`：允许字段表单。
- `frontend/src/components/ApprovalActions.vue`：批准、驳回和要求修改。
- `frontend/src/manualRevision.ts`：从父 revision 构造可信人工请求。

### 前端测试

- `frontend/src/test/setup.ts`：Vitest DOM 清理和 Element Plus 测试环境。
- `frontend/src/**/*.test.ts`：组件和纯函数测试，与被测模块放在同目录。
- `frontend/playwright.config.ts`：Chromium 和本地 Vite server。
- `frontend/tests/apiFixture.ts`：确定性 API 状态机。
- `frontend/tests/core-flow.spec.ts`：桌面主链路与要求修改链路。
- `frontend/tests/mobile-permissions.spec.ts`：手机权限和审批。

---

### Task 1: 当前用户与可恢复工作台 API

**Files:**
- Create: `backend/workbench.py`
- Create: `tests/test_workbench_api.py`
- Modify: `backend/schemas.py`
- Modify: `backend/routes.py`
- Modify: `tests/test_auth_and_scope.py`

**Interfaces:**
- Consumes: `get_current_user()`、`UserStoreScope`、`WorkflowRun`、`ProductProposal`、`ManualReviewRun`、现有 `WorkflowStatus/WorkflowType/WorkflowQuality/UserRole`。
- Produces: `GET /auth/me -> CurrentUserView`；`GET /workbench/tasks -> WorkbenchTaskListView`；`list_workbench_tasks(session, actor_id, page, page_size, store_id, kind, status)`。

- [ ] **Step 1: 写当前用户和工作台 RED 测试**

在 `tests/test_auth_and_scope.py` 增加：

```python
async def test_current_user_returns_database_identity(client, auth_data, operator_token) -> None:
    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {operator_token}"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": "operator-user",
        "username": "operator",
        "role": "operator",
    }
```

在 `tests/test_workbench_api.py` 使用现有 SQLite fixture 创建：

- 一个未选品的 `awaiting_selection` analysis；
- 一个已关联 proposal 的 analysis 和 optimization；
- 一个有活动 manual review 的 proposal；
- 一个其他店铺 proposal；
- operator、supervisor、admin 以及精确 `UserStoreScope`。

核心断言必须包含：

```python
assert response.status_code == 200
assert body["page"] == 1
assert body["page_size"] == 20
assert body["total"] == 3
assert [item["id"] for item in body["items"]] == [
    "proposal-needs-action",
    "analysis-awaiting-selection",
    "proposal-running",
]
assert body["items"][0]["action_required"] == "edit_proposal"
assert body["items"][0]["requires_current_user_action"] is True
assert "analysis-with-proposal" not in {item["id"] for item in body["items"]}
```

分别验证 `kind=proposal`、`status=pending_manual`、`store_id`、分页稳定次序、无 scope 不泄漏、停用用户 401、未知或越权店铺不返回跨店任务。验证 supervisor/admin 的 `pending_approval` 为 `review_approval + true`，operator 为 `wait + false`。

未知、停用和无 scope 的 `store_id` 筛选统一返回 404 `{"detail":{"code":"WORKBENCH_STORE_NOT_FOUND"}}`，避免通过 403 区分店铺是否存在。

- [ ] **Step 2: 运行 RED**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_auth_and_scope.py -k current_user -v
D:\E-commerce_operations_env\python.exe -m pytest tests/test_workbench_api.py -v
```

Expected: 失败于路由不存在或 `backend.workbench` 不存在；不得出现数据库、导入环境或旧测试失败。

- [ ] **Step 3: 增加精确响应 Schema**

在 `backend/schemas.py` 增加：

```python
class CurrentUserView(BaseModel):
    id: str
    username: str
    role: UserRole


class WorkbenchTaskView(BaseModel):
    id: str
    kind: Literal["analysis", "proposal"]
    store_id: str
    product_id: str | None
    analysis_run_id: str
    proposal_id: str | None
    workflow_run_id: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    action_required: Literal[
        "wait",
        "select_product",
        "edit_proposal",
        "submit_proposal",
        "review_approval",
        "view_result",
        "resolve_failure",
    ]
    requires_current_user_action: bool
    created_by: str
    updated_at: datetime


class WorkbenchTaskListView(BaseModel):
    items: list[WorkbenchTaskView]
    page: int
    page_size: int
    total: int
```

使用现有 UTC datetime serializer 约定，确保 SQLite naive datetime 输出带 UTC。

- [ ] **Step 4: 实现最小工作台读服务**

在 `backend/workbench.py` 定义：

```python
WorkbenchKind = Literal["analysis", "proposal"]
WorkbenchAction = Literal[
    "wait", "select_product", "edit_proposal", "submit_proposal",
    "review_approval", "view_result", "resolve_failure",
]


@dataclass(frozen=True)
class WorkbenchReadRow:
    id: str
    kind: WorkbenchKind
    store_id: str
    product_id: str | None
    analysis_run_id: str
    proposal_id: str | None
    workflow_run_id: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    quality_status: WorkflowQuality
    current_step: str | None
    action_required: WorkbenchAction
    requires_current_user_action: bool
    created_by: str
    updated_at: datetime


async def list_workbench_tasks(
    session: AsyncSession,
    *,
    actor_id: str,
    page: int,
    page_size: int,
    store_id: str | None,
    kind: WorkbenchKind | None,
    status: WorkflowStatus | None,
) -> tuple[list[WorkbenchReadRow], int]:
```

The function above is the exact public signature; its body implements the eight numbered rules below without adding another public service layer.

实现要求：

1. fresh-load active actor；查询 enabled Store 与精确 `UserStoreScope`，不扩大现有 proposal/approval 访问范围。
2. 一条 proposal 查询 join optimization workflow，并 outer join `ManualReviewRun` 与其 workflow alias；不要逐 proposal 发 N+1 查询。
3. 一条 analysis 查询读取授权店铺 analysis workflow；排除已出现在 proposal 查询中的 `analysis_run_id`。
4. proposal 有活动 manual workflow 时使用它作为 current workflow，否则使用 optimization workflow。
5. action 映射为纯函数 `_task_action(role, kind, status, has_active_manual)`；只有真实存在写接口的动作才把 `requires_current_user_action` 设为 true。
6. 在 Python 中合并两个小结果集，按 `not requires_action, updated_at DESC, id ASC` 排序，再应用筛选与分页。
7. 在合并处写明：

```python
# ponytail: local single-company task volumes are merged in memory;
# replace with a SQL UNION only when measured task volume makes this slow.
```

8. 捕获 SQLAlchemy 读取错误，rollback 后返回稳定 503 域错误；不得返回 lease、input、output、内部异常或幂等 hash。

定义 `WorkbenchDomainError(code: str, status_code: int)`；路由只把它转换为 `HTTPException(detail={"code": error.code})`。SQLAlchemy 失败统一为 `WORKBENCH_READ_FAILED` 503。

action 映射必须精确遵循下表：

| kind / 状态 | role / 条件 | action | requires action |
|---|---|---|---:|
| analysis accepted/processing | 所有角色 | wait | false |
| analysis awaiting_selection | operator | select_product | true |
| analysis awaiting_selection | supervisor/admin | view_result | false |
| analysis completed | 所有角色 | view_result | false |
| analysis failed | 所有角色 | resolve_failure | false |
| proposal 任意状态 | 有活动 manual workflow | wait | false |
| proposal accepted/processing | 所有角色 | wait | false |
| proposal draft_ready | 所有角色 | submit_proposal | true |
| proposal pending_manual | 所有角色 | edit_proposal | true |
| proposal pending_approval | supervisor/admin | review_approval | true |
| proposal pending_approval | operator | wait | false |
| proposal completed/rejected | 所有角色 | view_result | false |
| proposal failed | 所有角色 | resolve_failure | false |

- [ ] **Step 5: 接入路由并运行 GREEN**

在 `backend/routes.py` 接入两个 GET。工作台 Query 使用 `page >= 1`、`1 <= page_size <= 100`，`kind` 为字面量，`status` 为现有枚举。

```python
@router.get("/auth/me", response_model=CurrentUserView)
async def read_current_user(user: User = Depends(get_current_user)) -> CurrentUserView:
    return CurrentUserView(id=user.id, username=user.username, role=user.role)
```

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_auth_and_scope.py tests/test_workbench_api.py -v
D:\E-commerce_operations_env\python.exe -m pytest tests/test_analysis_api.py tests/test_optimization_api.py tests/test_approval_api.py -v
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
```

Expected: 全部通过；无迁移或模型文件变化。

- [ ] **Step 6: 精确提交并停止审查**

```powershell
git add -- backend/workbench.py backend/schemas.py backend/routes.py tests/test_workbench_api.py tests/test_auth_and_scope.py
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add recoverable workbench reads"
git show --check --stat --oneline HEAD
git status --short
```

Expected staged paths exactly match the five paths above。向审查窗口返回提交哈希、测试通过数、skip 数和 clean status；等待放行 Task 2。

---

### Task 2: Vue 工程、认证、外壳与同源托管

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/package-lock.json`
- Create: `frontend/index.html`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tsconfig.app.json`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/router.ts`
- Create: `frontend/src/api.ts`
- Create: `frontend/src/session.ts`
- Create: `frontend/src/types.ts`
- Create: `frontend/src/capabilities.ts`
- Create: `frontend/src/status.ts`
- Create: `frontend/src/styles.css`
- Create: `frontend/src/pages/LoginPage.vue`
- Create: `frontend/src/pages/ForbiddenPage.vue`
- Create: `frontend/src/pages/WorkbenchPage.vue`
- Create: `frontend/src/components/AppShell.vue`
- Create: `frontend/src/components/StatusTag.vue`
- Create: `frontend/src/components/InlineError.vue`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/session.test.ts`
- Create: `frontend/src/pages/LoginPage.test.ts`
- Create: `tests/test_frontend_hosting.py`
- Modify: `.gitignore`
- Modify: `backend/main.py`

**Interfaces:**
- Consumes: Task 1 `GET /auth/me`，现有 `POST /auth/login`，现有 API 根路径。
- Produces: `apiRequest<T>()`、`session`、`restoreSession()`、精确 action capability 函数、`/app/login` 和受保护 shell；FastAPI `/app` 静态托管。

- [ ] **Step 1: 固定依赖、命令和忽略项**

先用 `apply_patch` 创建以下 `frontend/package.json`，不要运行交互式脚手架：

```json
{
  "name": "ecommerce-operations-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "typecheck": "vue-tsc --noEmit",
    "test": "vitest run",
    "test:e2e": "playwright test",
    "build": "npm run typecheck && vite build"
  }
}
```

然后在 PowerShell 设置 D 盘缓存并生成锁文件：

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:npm_config_cache='D:\E-commerce_operations_env\npm-cache'
Set-Location "$repoRoot\frontend"
npm install --save-exact vue@3.5.42 vue-router@5.3.0 element-plus@2.14.5 @element-plus/icons-vue@2.3.2
npm install --save-dev --save-exact vite@8.2.2 typescript@7.0.2 @vitejs/plugin-vue@6.0.8 vue-tsc@3.3.11 vitest@4.1.11 @vue/test-utils@2.5.0 jsdom@30.0.1 @playwright/test@1.62.1
```

安装后 `package.json` 保留上述 scripts，dependencies/devDependencies 使用命令中的精确版本，`package-lock.json` 由 npm 生成。

`.gitignore` 增加：

```gitignore
frontend/node_modules/
frontend/dist/
frontend/coverage/
frontend/playwright-report/
frontend/test-results/
```

- [ ] **Step 2: 写认证、权限和托管 RED 测试**

`session.test.ts` 验证 Token 只写入 `sessionStorage`、clear 清空用户和 Token。`LoginPage.test.ts` mock `api.login`，验证 label、提交禁用、统一错误、成功后进入 `/app/workbench`。

`tests/test_frontend_hosting.py` 使用 `tmp_path` 写一个最小 `index.html` 和 `assets/app.js`，调用 `create_app(frontend_dist=tmp_path)`，断言：

```python
assert (await client.get("/app")).text == "frontend-index"
assert (await client.get("/app/proposals/example")).text == "frontend-index"
assert (await client.get("/app/assets/app.js")).text == "asset"
assert (await client.get("/health/live")).json() == {"status": "ok"}
assert (await client.get("/stores")).status_code == 401
```

- [ ] **Step 3: 运行 RED**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:npm_config_cache='D:\E-commerce_operations_env\npm-cache'
Set-Location "$repoRoot\frontend"
npm run test -- src/session.test.ts src/pages/LoginPage.test.ts
Set-Location $repoRoot
D:\E-commerce_operations_env\python.exe -m pytest tests/test_frontend_hosting.py -v
```

Expected: 前端模块和 `create_app(frontend_dist=tmp_path)` 尚不存在而失败。

- [ ] **Step 4: 实现严格 TypeScript 基础**

`tsconfig.app.json` 打开 `strict`、`noUncheckedIndexedAccess`、`exactOptionalPropertyTypes` 和 `noEmit`。`vite.config.ts` 使用 `/app/` base，并把这些根前缀代理到 `http://127.0.0.1:8000`：`/auth`、`/stores`、`/analysis-runs`、`/workflow-runs`、`/workbench`、`/proposals`、`/approvals`。

`vite.config.ts` 的测试配置固定为：

```ts
export default defineConfig({
  base: '/app/',
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
  server: { proxy: apiProxy },
})
```

`apiProxy` 为上述七个根前缀逐项映射同一个 target，`changeOrigin: false`；不增加 `/api` 重写。

`session.ts` 的公开接口固定为：

```ts
export type SessionState = {
  token: string | null
  user: CurrentUser | null
  ready: boolean
}

export const session: SessionState
export function setToken(token: string): void
export function setCurrentUser(user: CurrentUser): void
export function clearSession(): void
```

`api.ts` 固定为一个网络边界：

```ts
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly requestId?: string,
    public readonly fieldErrors: Record<string, string> = {},
  ) { super(code) }
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T>

export async function login(username: string, password: string): Promise<void>
export async function restoreSession(): Promise<void>
```

要求：Authorization 来自 `session.token`；GET 使用 `cache: "no-store"`；204 不解析 JSON；错误兼容字符串 `detail`、`detail.code`、FastAPI validation array 和 knowledge envelope；401 调用 `clearSession()`。

`capabilities.ts` 只提供实际使用的判断：

```ts
export function canStartAnalysis(role: UserRole, isMobile: boolean): boolean
export function canSelectProduct(role: UserRole, isMobile: boolean): boolean
export function canEditProposal(role: UserRole, status: WorkflowStatus, isMobile: boolean): boolean
export function canSubmitProposal(role: UserRole, status: WorkflowStatus, isMobile: boolean): boolean
export function canApprove(role: UserRole, status: WorkflowStatus): boolean
```

当前后端选品只允许 operator，因此 `canStartAnalysis` 与 `canSelectProduct` 在桌面/平板只对 operator 返回 true；审批能力只取决于 supervisor/admin 与 `pending_approval`，手机端不禁用审批。

`status.ts` 集中定义中文标签、Element Plus tag type 和下一动作文案；组件不得自建第二份映射。

- [ ] **Step 5: 实现登录、路由、外壳和浅色 token**

路由使用 `/app` base，meta 只保留 `requiresAuth` 与 `approvalOnly`。启动顺序为 `await restoreSession()`、创建 router、mount。守卫对 401 返回 `/app/login`，对 approvalOnly 非审批角色返回命名 `forbidden` 页面。

`styles.css` 定义：

```css
:root {
  --el-color-primary: #0f766e;
  --app-bg: #f4f6f8;
  --app-surface: #ffffff;
  --app-text: #1f2937;
  --app-muted: #64748b;
  --app-radius: 6px;
  color-scheme: light;
  font-family: "Microsoft YaHei UI", "PingFang SC", "Noto Sans SC", system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
}
```

所有按钮、输入和 focus ring 保持 WCAG AA；不得加入渐变、外发光、玻璃效果或无语义动画。

Task 2 的 `WorkbenchPage.vue` 只提供受保护登录落点：一个“任务工作台”标题和安全空状态，不请求假数据。Task 3 在同一文件加入真实列表，因此 Task 2 的登录导航和生产构建都可独立验收。

- [ ] **Step 6: 实现 `/app` 同源托管**

把 `create_app` 改为：

```python
def create_app(frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="智营台 API", version="0.1.0")
    app.include_router(router)
    # existing exception handlers and health route remain unchanged
    _mount_frontend(app, frontend_dist or DEFAULT_FRONTEND_DIST)
    return app
```

`_mount_frontend` 仅在目录和 `index.html` 存在时注册 `/app/assets` StaticFiles、`/app` 和 `/app/{path:path}`。fallback 始终返回 index；assets 缺失返回真实 404，不回退到 index。API 与 health 在 fallback 前注册且原路径不变。

- [ ] **Step 7: 运行 GREEN 和回归**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:npm_config_cache='D:\E-commerce_operations_env\npm-cache'
Set-Location "$repoRoot\frontend"
npm run typecheck
npm run test -- src/session.test.ts src/pages/LoginPage.test.ts
npm run build
Set-Location $repoRoot
D:\E-commerce_operations_env\python.exe -m pytest tests/test_frontend_hosting.py tests/test_health.py tests/test_auth_and_scope.py -v
git diff --check
```

Expected: 全部通过；`frontend/dist` 存在但未被 Git 跟踪。

- [ ] **Step 8: 精确提交并停止审查**

```powershell
git add -- .gitignore backend/main.py tests/test_frontend_hosting.py frontend/package.json frontend/package-lock.json frontend/index.html frontend/vite.config.ts frontend/tsconfig.json frontend/tsconfig.app.json frontend/src/main.ts frontend/src/App.vue frontend/src/router.ts frontend/src/api.ts frontend/src/session.ts frontend/src/types.ts frontend/src/capabilities.ts frontend/src/status.ts frontend/src/styles.css frontend/src/pages/LoginPage.vue frontend/src/pages/ForbiddenPage.vue frontend/src/pages/WorkbenchPage.vue frontend/src/components/AppShell.vue frontend/src/components/StatusTag.vue frontend/src/components/InlineError.vue frontend/src/test/setup.ts frontend/src/session.test.ts frontend/src/pages/LoginPage.test.ts
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add authenticated Vue shell"
git show --check --stat --oneline HEAD
git status --short
```

Expected: 只出现上述路径，build 产物不在 staged paths。等待放行 Task 3。

---

### Task 3: 任务工作台

**Files:**
- Modify: `frontend/src/pages/WorkbenchPage.vue`
- Create: `frontend/src/pages/WorkbenchPage.test.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/components/AppShell.vue`

**Interfaces:**
- Consumes: `GET /workbench/tasks`、`GET /stores`、Task 2 session/status/error components。
- Produces: `/app/workbench` 与 `/app/proposals` 两个视图；`listWorkbenchTasks(query)`。

- [ ] **Step 1: 写工作台 RED 组件测试**

mock `listWorkbenchTasks` 和 `listStores`，覆盖 loading、系统空、筛选空、API error、任务表、手机摘要行。精确断言：

```ts
expect(wrapper.get('[data-test="needs-action-count"]').text()).toContain('2')
expect(wrapper.get('[data-test="task-proposal-1"]').text()).toContain('等待审批')
expect(wrapper.get('[data-test="continue-proposal-1"]').attributes('href'))
  .toBe('/app/proposals/proposal-1')
```

验证 `/app/proposals` 自动发送 `kind=proposal`，筛选变化重置 page=1，失败重试保留筛选。顶部数字必须标注“当前页”，只从当前 items 计算，禁止伪装成全库计数。

- [ ] **Step 2: 运行 RED**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:npm_config_cache='D:\E-commerce_operations_env\npm-cache'
Set-Location "$repoRoot\frontend"
npm run test -- src/pages/WorkbenchPage.test.ts
```

Expected: 页面或 API 函数不存在而失败。

- [ ] **Step 3: 实现最小工作台**

在 `types.ts` 定义与后端完全同名的 `WorkbenchTask`、`WorkbenchTaskList`、`StoreSummary`。`api.ts` 增加：

```ts
export async function listWorkbenchTasks(
  query: { page: number; pageSize: number; storeId?: string; kind?: TaskKind; status?: WorkflowStatus },
  signal?: AbortSignal,
): Promise<WorkbenchTaskList>

export async function listStores(signal?: AbortSignal): Promise<StoreSummary[]>
```

页面只维护一个列表请求，不为各行轮询。任务链接规则：analysis -> `/app/analysis/:analysisRunId`，proposal -> `/app/proposals/:proposalId`。店铺名称从已有 `/stores` 映射；proposal 商品名称按需在详情页读取，不在列表页新增 N+1 产品请求。

桌面使用 Element Plus table；手机使用同一 items 的语义摘要行。状态、quality 和 action 文案全部来自 `status.ts`。

- [ ] **Step 4: GREEN、类型检查和提交**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location "$repoRoot\frontend"
npm run test -- src/pages/WorkbenchPage.test.ts
npm run typecheck
npm run build
Set-Location $repoRoot
git diff --check
git add -- frontend/src/pages/WorkbenchPage.vue frontend/src/pages/WorkbenchPage.test.ts frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/components/AppShell.vue
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add task workbench UI"
git show --check --stat --oneline HEAD
git status --short
```

Expected: 只提交六个路径。等待放行 Task 4。

---

### Task 4: 经营分析、轮询与候选商品

**Files:**
- Create: `frontend/src/pages/AnalysisPage.vue`
- Create: `frontend/src/pages/AnalysisPage.test.ts`
- Create: `frontend/src/pages/AnalysisRunPage.vue`
- Create: `frontend/src/pages/AnalysisRunPage.test.ts`
- Create: `frontend/src/components/EvidenceDrawer.vue`
- Create: `frontend/src/useSerialPoll.ts`
- Create: `frontend/src/useSerialPoll.test.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/components/AppShell.vue`

**Interfaces:**
- Consumes: stores、analysis-runs、workflow status、candidates、select-product 现有 API。
- Produces: `/app/analysis`、`/app/analysis/:runId`、`useSerialPoll(load, isTerminal, onValue)`。

- [ ] **Step 1: 写轮询和页面 RED 测试**

`useSerialPoll.test.ts` 使用 fake timers 证明：请求不重叠；成功后 2 秒；连续两次网络失败后 5 秒；下一次成功恢复 2 秒；终态和 unmount 停止并 abort。

页面测试覆盖 1 天和 90 天合法、91 天禁用、202 响应导航、processing 骨架、awaiting_selection 候选表、证据纯文本、选品二次确认和幂等重试。

```ts
expect(selectProduct).toHaveBeenCalledWith(
  'analysis-1',
  'candidate-1',
  expect.stringMatching(/^[0-9a-f-]{36}$/),
)
```

手机 viewport 下候选详情可读，但 select button 不渲染。

- [ ] **Step 2: 运行 RED**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location "$repoRoot\frontend"
npm run test -- src/useSerialPoll.test.ts src/pages/AnalysisPage.test.ts src/pages/AnalysisRunPage.test.ts
```

Expected: 新模块不存在而失败。

- [ ] **Step 3: 实现 API、轮询和页面**

`api.ts` 增加实际签名：

```ts
export function createAnalysisRun(body: AnalysisRunRequest): Promise<AnalysisRunAccepted>
export function getWorkflowRun(id: string, signal?: AbortSignal): Promise<WorkflowRun>
export function listAnalysisCandidates(id: string, signal?: AbortSignal): Promise<AnalysisCandidate[]>
export function selectProduct(
  analysisRunId: string,
  candidateId: string,
  idempotencyKey: string,
): Promise<ProductSelection>
```

日期值使用 `YYYY-MM-DD` 字符串，不经本地时区 Date 序列化。选品写请求的 idempotency key 在组件状态内生成一次，超时重试复用；成功后清除并导航 proposal。

`EvidenceDrawer.vue` 只用 `{{ evidence }}` 文本绑定，不使用 `v-html`。候选指标直接显示真实 Decimal 字符串，不生成假趋势和假百分比。

- [ ] **Step 4: GREEN、回归和提交**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location "$repoRoot\frontend"
npm run test -- src/useSerialPoll.test.ts src/pages/AnalysisPage.test.ts src/pages/AnalysisRunPage.test.ts
npm run typecheck
npm run build
Set-Location $repoRoot
D:\E-commerce_operations_env\python.exe -m pytest tests/test_analysis_api.py tests/test_optimization_api.py -v
git diff --check
git add -- frontend/src/pages/AnalysisPage.vue frontend/src/pages/AnalysisPage.test.ts frontend/src/pages/AnalysisRunPage.vue frontend/src/pages/AnalysisRunPage.test.ts frontend/src/components/EvidenceDrawer.vue frontend/src/useSerialPoll.ts frontend/src/useSerialPoll.test.ts frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/components/AppShell.vue
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add analysis workflow UI"
git show --check --stat --oneline HEAD
git status --short
```

Expected: 只提交十一个路径。等待放行 Task 5。

---

### Task 5: 方案差异、合规结果与人工修订

**Files:**
- Create: `frontend/src/pages/ProposalPage.vue`
- Create: `frontend/src/pages/ProposalPage.test.ts`
- Create: `frontend/src/components/ProposalDiff.vue`
- Create: `frontend/src/components/CompliancePanel.vue`
- Create: `frontend/src/components/ManualRevisionForm.vue`
- Create: `frontend/src/manualRevision.ts`
- Create: `frontend/src/manualRevision.test.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/router.ts`

**Interfaces:**
- Consumes: `GET /proposals/{id}`、`POST /proposals/{id}/manual-revision`、`POST /proposals/{id}/submit`、Task 4 serial poll。
- Produces: `/app/proposals/:id`；`buildManualRevisionRequest(detail, form)`。

- [ ] **Step 1: 写可信人工请求 RED 纯函数测试**

fixture 包含 title、selling_points、description、keywords 四条父 `changes`、attribute completions、citations、price suggestions 和 SKU suggestions。验证：

```ts
const result = buildManualRevisionRequest(detail, form)
expect(result.unsupported).toEqual([])
expect(result.request?.parent_revision_id).toBe('revision-2')
expect(result.request?.base_product_version).toBe(7)
expect(result.request?.changes[0]).toMatchObject({
  field: 'title',
  current_value: '原商品标题',
  suggested_value: '人工修订标题',
  reason: '优化标题表达',
  evidence: [{ kind: 'fact', value: 'product.title' }],
})
expect(result.request).not.toHaveProperty('citations')
expect(result.request).not.toHaveProperty('price_suggestions')
expect(result.request).not.toHaveProperty('sku_suggestions')
```

测试不能修改输入对象。父 revision 缺少某字段的 current value、reason 或 evidence 时，helper 返回字段级 `unsupported`，该字段保持只读；不得伪造 evidence。

- [ ] **Step 2: 写方案页面 RED 测试**

覆盖 skeleton、partial error、五阶段时间线、桌面左右/平板上下、合规 required changes、纯文本 citation、价格/SKU 只读、draft_ready submit、pending_manual edit、pending_approval disabled reason。

验证 manual POST 不含三组只读字段，写请求 pending 时按钮 disabled，同键 retry，manual workflow 终态后刷新 proposal。验证手机不渲染 edit/submit。

- [ ] **Step 3: 运行 RED**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location "$repoRoot\frontend"
npm run test -- src/manualRevision.test.ts src/pages/ProposalPage.test.ts
```

Expected: helper 与页面不存在而失败。

- [ ] **Step 4: 实现 typed proposal 和可信构造器**

在 `types.ts` 精确声明 `ProposalDetail`、`ProposalRevision`、`ComplianceReview`、`DescriptionSection`、`OptimizationChange`、`AttributeCompletion`、`PriceSuggestion`、`SkuSuggestion`、`ManualRevisionRequest`。不得用 `any`；后端开放 JSON 的不可枚举细节使用 `unknown` 并在组件入口收窄。

构造器接口固定为：

```ts
export type EditableProposalField =
  | 'title'
  | 'selling_points'
  | 'description'
  | 'keywords'
  | 'attribute_completions'

export type ManualRevisionBuildResult = {
  request: ManualRevisionRequest | null
  unsupported: EditableProposalField[]
}

export function buildManualRevisionRequest(
  detail: ProposalDetail,
  form: ManualRevisionFormValue,
): ManualRevisionBuildResult
```

`buildManualRevisionRequest` 规则：

1. editable 值来自 form；parent ID/base version 来自当前 revision。
2. 四个 content change 仅复用父 change 的 `current_value`、`reason`、`evidence`，只替换 `suggested_value`。
3. evidence 或可信 current value 缺失时不生成该 change，返回明确 unsupported 字段。
4. attributes 只允许编辑父 revision 已存在且有 evidence 的 completion；不允许在浏览器凭空增加无证据属性。
5. 永远不复制 citations、price suggestions 或 SKU suggestions 到请求。

- [ ] **Step 5: 实现详情、差异、合规和修订**

时间线只从当前服务端事实派生，不伪造完整审计历史。`ProposalDiff` 对每个 `changes` 显示 `current_value` 与 `suggested_value`；price/SKU 放在“只读建议”区域并显示“本阶段不会应用”。

表单边界与 Pydantic 保持一致：标题 1-60 且含中文；卖点 1-5、每项 1-80；详情 1-10、heading 1-40、body 1-1000；关键词 1-20、每项 1-32；属性最多 20；changes 最多 4。

`api.ts` 增加：

```ts
export function getProposal(id: string, signal?: AbortSignal): Promise<ProposalDetail>
export function createManualRevision(
  proposalId: string,
  body: ManualRevisionRequest,
  idempotencyKey: string,
): Promise<ManualRevisionAccepted>
export function submitProposal(
  proposalId: string,
  revisionId: string,
  idempotencyKey: string,
): Promise<ApprovalAction>
```

- [ ] **Step 6: GREEN、后端契约回归和提交**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location "$repoRoot\frontend"
npm run test -- src/manualRevision.test.ts src/pages/ProposalPage.test.ts
npm run typecheck
npm run build
Set-Location $repoRoot
D:\E-commerce_operations_env\python.exe -m pytest tests/test_manual_review_api.py tests/test_optimization_api.py tests/test_approval_api.py -v
git diff --check
git add -- frontend/src/pages/ProposalPage.vue frontend/src/pages/ProposalPage.test.ts frontend/src/components/ProposalDiff.vue frontend/src/components/CompliancePanel.vue frontend/src/components/ManualRevisionForm.vue frontend/src/manualRevision.ts frontend/src/manualRevision.test.ts frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add proposal revision UI"
git show --check --stat --oneline HEAD
git status --short
```

Expected: 只提交十个路径。等待放行 Task 6。

---

### Task 6: 审批、模拟发布结果与移动端限制

**Files:**
- Create: `frontend/src/pages/ApprovalsPage.vue`
- Create: `frontend/src/pages/ApprovalsPage.test.ts`
- Create: `frontend/src/components/ApprovalActions.vue`
- Create: `frontend/src/components/ApprovalActions.test.ts`
- Create: `frontend/src/capabilities.test.ts`
- Modify: `frontend/src/pages/ProposalPage.vue`
- Modify: `frontend/src/pages/ProposalPage.test.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/capabilities.ts`
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/components/AppShell.vue`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `/approvals`、approve/reject/request-changes、proposal detail publish record。
- Produces: `/app/approvals`、`ApprovalActions`、统一 desktop/tablet/mobile capability rules。

- [ ] **Step 1: 写权限和审批 RED 测试**

`capabilities.test.ts` 使用表驱动覆盖三种 role、mobile true/false 和状态。硬断言：operator 永远不能 approve；supervisor/admin 可 self-approve；mobile 永远不能 create analysis、select、edit、submit。

`ApprovalsPage.test.ts` 覆盖分页、空、403、进入 proposal。`ApprovalActions.test.ts` 覆盖批准确认、驳回/要求修改 1-500 字、pending 禁用、同键重试、成功刷新。

```ts
expect(approveProposal).toHaveBeenCalledWith(
  'proposal-1',
  'revision-2',
  expect.any(String),
)
expect(requestChanges).not.toHaveBeenCalledWith(
  expect.anything(), expect.objectContaining({ comment: '' }), expect.anything(),
)
```

- [ ] **Step 2: 运行 RED**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location "$repoRoot\frontend"
npm run test -- src/capabilities.test.ts src/pages/ApprovalsPage.test.ts src/components/ApprovalActions.test.ts src/pages/ProposalPage.test.ts
```

Expected: 新页面和 action API 不存在而失败。

- [ ] **Step 3: 实现审批 API 和页面**

`api.ts` 增加：

```ts
export function listApprovals(page: number, pageSize: number, signal?: AbortSignal): Promise<ApprovalList>
export function approveProposal(id: string, revisionId: string, key: string): Promise<PublishRecord>
export function rejectProposal(id: string, revisionId: string, comment: string, key: string): Promise<ApprovalAction>
export function requestProposalChanges(id: string, revisionId: string, comment: string, key: string): Promise<ApprovalAction>
```

`ApprovalActions` 接收 `proposalId`、`revisionId`、`actorRole`、`pending` 和成功 callback。批准使用确认框；reject/request changes 使用带 label 的 dialog。所有写操作使用独立 key 状态，不能跨 action 复用。

成功后 proposal 页面重新 GET，不在前端手工拼 publish record。发布区显示 before/after 允许字段、base/published version，并固定说明价格、SKU、库存和真实平台未变化。

- [ ] **Step 4: 实现响应式能力**

使用一个 CSS media query `max-width: 767px` 配合 `capabilities.ts`。手机隐藏发起分析、选品、修订和提交；审批角色仍显示待审批列表、只读详情和固定底部 `ApprovalActions`。平板 `768-1023px` 保留全部操作并把 diff 改为上下布局。

禁用动作必须提供原因文本；不能只把按钮变灰。focus order、dialog focus return 和 `aria-live` 状态区域进入组件测试。

- [ ] **Step 5: GREEN、审批回归和提交**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location "$repoRoot\frontend"
npm run test -- src/capabilities.test.ts src/pages/ApprovalsPage.test.ts src/components/ApprovalActions.test.ts src/pages/ProposalPage.test.ts
npm run typecheck
npm run build
Set-Location $repoRoot
D:\E-commerce_operations_env\python.exe -m pytest tests/test_approval_api.py tests/test_manual_review_flow.py -v
git diff --check
git add -- frontend/src/pages/ApprovalsPage.vue frontend/src/pages/ApprovalsPage.test.ts frontend/src/components/ApprovalActions.vue frontend/src/components/ApprovalActions.test.ts frontend/src/capabilities.test.ts frontend/src/pages/ProposalPage.vue frontend/src/pages/ProposalPage.test.ts frontend/src/api.ts frontend/src/types.ts frontend/src/capabilities.ts frontend/src/router.ts frontend/src/components/AppShell.vue frontend/src/styles.css
git diff --cached --name-only
git diff --cached --check
git commit -m "feat: add responsive approval flow"
git show --check --stat --oneline HEAD
git status --short
```

Expected: 只提交十三个路径。等待放行 Task 7。

---

### Task 7: 浏览器链路、生产构建与完整回归

**Files:**
- Create: `frontend/playwright.config.ts`
- Create: `frontend/tests/apiFixture.ts`
- Create: `frontend/tests/core-flow.spec.ts`
- Create: `frontend/tests/mobile-permissions.spec.ts`

**Interfaces:**
- Consumes: Tasks 1-6 全部页面和 API contracts。
- Produces: 确定性 Chromium 前端链路、最终类型/构建/回归证据。

- [ ] **Step 1: 安装 D 盘 Chromium 并写 RED 浏览器测试**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:npm_config_cache='D:\E-commerce_operations_env\npm-cache'
$env:PLAYWRIGHT_BROWSERS_PATH='D:\E-commerce_operations_env\playwright-browsers'
Set-Location "$repoRoot\frontend"
npx playwright install chromium
```

`playwright.config.ts` 只启用 Chromium，`baseURL=http://127.0.0.1:4173/app/`，webServer 使用 `npm run dev -- --host 127.0.0.1 --port 4173`，reuseExistingServer=false。

`apiFixture.ts` 用 `page.route` 实现确定性内存状态机，不访问真实 DeepSeek、PostgreSQL 或 Milvus：login -> workbench -> analysis accepted/processing/awaiting_selection -> selection -> proposal pending_manual/draft_ready -> submit -> pending_approval -> request_changes 或 approve -> completed publish record。每个受保护 handler 验证 Authorization，每个写 handler 另外验证 `Idempotency-Key`。

- [ ] **Step 2: 定义桌面和手机验收**

`core-flow.spec.ts` 包含两个独立测试：

1. operator 登录、发起分析、候选选品、人工修订、提交；supervisor 重新登录并自审批准；断言本地模拟发布版本从 7 到 8，页面显示价格/SKU/库存未变。
2. supervisor 提交后 request changes，重新修订、复核、再次提交并批准；断言第一次 revision 仍可读。

`mobile-permissions.spec.ts` 使用 390x844 viewport，断言 operator 可看任务但不存在 create/select/edit/submit；supervisor 可看 proposal 摘要并执行审批。

所有 selector 使用 role、label 或稳定 `data-test`，不依赖 Element Plus 生成 class。

- [ ] **Step 3: 运行 RED 并修复测试可观测性**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:PLAYWRIGHT_BROWSERS_PATH='D:\E-commerce_operations_env\playwright-browsers'
Set-Location "$repoRoot\frontend"
npm run test:e2e
```

Expected: 测试最初因 API fixture 尚未覆盖完整状态转换而失败。Tasks 3-6 已随组件加入计划中列明的稳定 `data-test`，Task 7 不再修改业务组件。

- [ ] **Step 4: 运行全部前端门禁**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
$env:npm_config_cache='D:\E-commerce_operations_env\npm-cache'
$env:PLAYWRIGHT_BROWSERS_PATH='D:\E-commerce_operations_env\playwright-browsers'
Set-Location "$repoRoot\frontend"
npm run typecheck
npm run test
npm run test:e2e
npm run build
```

Expected: typecheck、全部 Vitest、三个 Playwright 场景和生产构建通过。记录准确 test 数量和耗时。

- [ ] **Step 5: 运行后端和 Git 最终门禁**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location $repoRoot
D:\E-commerce_operations_env\python.exe -m pytest
D:\E-commerce_operations_env\python.exe -m compileall backend tests
git diff --check
git status --short
git ls-files frontend/dist frontend/node_modules frontend/playwright-report frontend/test-results
```

Expected: 完整 pytest 零失败；最后一条 `git ls-files` 无输出；只有四个 Task 7 新文件处于未提交状态。

- [ ] **Step 6: 最终视觉与权限人工 smoke**

在 1440x900、900x1200 和 390x844 三个 viewport 检查：

- 导航单行/折叠正确，无横向页面滚动。
- loading、empty、error、disabled、degraded 和 replay 有可见文案。
- 状态不只靠颜色；focus 可见；弹窗 focus 返回。
- 手机无创建、选品、修订或提交入口；主管审批仍可用。
- price/SKU 明确只读；发布明确为本地模拟。
- 无渐变标题、玻璃效果、外发光、卡片墙或装饰动画。

- [ ] **Step 7: 精确提交并交回最终审查**

```powershell
$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location $repoRoot
git add -- frontend/playwright.config.ts frontend/tests/apiFixture.ts frontend/tests/core-flow.spec.ts frontend/tests/mobile-permissions.spec.ts
git diff --cached --name-only
git diff --cached --check
git commit -m "test: verify frontend operations flow"
git show --check --stat --oneline HEAD
git status --short
```

Expected: staged paths 只包含四个 Playwright 文件。向审查窗口返回七个提交的完整链、所有测试精确统计、三个 viewport smoke 结果、最终 `git status --short` 和未夸大的交付声明。

---

## Final Acceptance Checklist

- [ ] `GET /auth/me` 只返回当前数据库身份，失效用户 401。
- [ ] `GET /workbench/tasks` 按精确 scope 归并 analysis/proposal，跨刷新可恢复且不泄漏内部字段。
- [ ] Vue shell 使用 sessionStorage Bearer JWT，无 Pinia/Axios/Tailwind/图表库。
- [ ] 工作台、经营分析、候选选品、方案详情、人工修订、提交和审批页面全部可达。
- [ ] operator 不能审批；supervisor/admin 可以自审；服务端仍执行实时权限校验。
- [ ] 价格、SKU、库存和引用证据只读，人工请求不提交只读组。
- [ ] 桌面和平板完整，手机只读任务和主管/管理员审批。
- [ ] loading、empty、filtered empty、error、disabled、degraded 和 replay 均有测试。
- [ ] 串行轮询无并发、终态停止、卸载 abort、失败退避后恢复。
- [ ] FastAPI `/app` SPA fallback 不捕获 API、health 或缺失 asset。
- [ ] TypeScript noEmit、Vitest、Playwright、生产构建、静态 smoke 和完整 pytest 全部通过。
- [ ] dist、node_modules、browser binaries、coverage 和测试报告均未提交且不写入 C 盘项目缓存。
- [ ] 浏览器测试只声明为 fixture 驱动前端链路，真实全栈 E2E 仍明确留在第 10 阶段。
