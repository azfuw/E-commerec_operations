# 智营台

## 物流部门工作台

新增 **智流物流协同工作台**，入口为 `/app/logistics`，也可从原工作台侧栏进入。包括运单检索与完整轨迹、承诺发货/实际发货/预计到货/实际签收时间、退货进度、异常任务分配与处理、规则巡检 Agent、带运单依据的业务问答及 CSV 导出。

本机体验（PowerShell，仓库根目录）：

```powershell
.\start-logistics.ps1
```

打开 `http://127.0.0.1:8010/app/logistics`，使用演示账号 `logistics`、密码 `Logistics!2026`。该主管账号能查看三个演示店铺；`warehouse` 使用相同演示密码，仅能查看旗舰店。启动脚本优先复用本机已经安装的 Python 3.11 环境，也可传入 `-Python 'D:\your-env\python.exe'`。

演示只监听本机，数据库独立保存在 `data/logistics-demo/logistics.db`，首次启动生成数据，重启保留录入和处理记录。无需 Docker、Milvus 或外部模型密钥。Python 环境需包含项目依赖和测试依赖中的 `aiosqlite`；前端沿用原项目依赖。

普通部署继续使用现有 PostgreSQL 配置，执行 `python -m alembic upgrade head`、构建前端并启动 FastAPI。通过界面将已有订单登记为运单即可开始积累真实内部业务数据。演示账号和演示库不会自动写入普通部署数据库。

当前 Agent 是可解释的本地规则模式；承运商轨迹由手工录入或演示数据提供，尚未连接真实物流平台。退货完成表示物流和验收闭环，不触发实际退款。操作与验收说明见 [物流使用指南](docs/logistics-guide.md)。

## 项目定位

面向国内多店铺电商运营的本地演示系统：从经营指标发现异常，生成有可信事实和知识引用支持的商品文案，再经过合规检查、人工修订和主管审批完成本地模拟发布。

## 已实现能力

- 多店铺经营概览、商品异常分析、任务工作台与店铺权限隔离。
- 分析、商品优化和合规 Agent；服务端校验模型输出与可信输入的一致性。
- PostgreSQL 持久任务、租约领取、检查点恢复和有界重试。
- 知识上传、版本激活与停用、本地 BGE 向量化、Milvus 混合检索与重排。
- 不可变方案修订、人工复核、主管批准／驳回／退回修改，以及安全审计。
- 管理控制台、Agent 调用元数据、离线评测和审计查询。
- 审批事务原子创建本地发布记录和平台投递任务；平台投递支持幂等重放、限流、失败状态和签名 Webhook。

## 系统边界

平台目标为本地 `contract_simulator`（契约模拟器）。未取得或验证真实商家认证、正式 OAuth 授权和生产平台 API，没有向真实店铺发布商品。

价格、SKU、库存和订单不属于可写发布字段。模型给出的价格或 SKU 建议不会直接修改这些事实。普通测试和真实服务浏览器 E2E 使用本地确定性模型；真实 DeepSeek 验证使用合成输入并单独显式开启。

## 架构

Vue 3 管理界面通过 FastAPI 访问业务 API。PostgreSQL 保存业务事实、修订、审批、审计、任务租约和 LangGraph 检查点。五类独立 Worker 执行分析、知识入库、优化／合规、人工复核和平台投递。Milvus 与本地 BGE 模型提供知识检索；模型调用和平台 HTTP 客户端位于各自明确的外部边界。

详细设计见 [第九阶段管理控制台规格](docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md)和[第十阶段生产化验证规格](docs/superpowers/specs/2026-09-07-phase-10-production-validation-design.md)。

## 角色与权限

| 角色 | 主要职责 |
|---|---|
| 运营 `operator` | 在授权店铺查看经营数据、发起分析、选择商品、修订方案和提交审批 |
| 主管 `supervisor` | 在授权店铺检查待审方案，批准、驳回或退回修改 |
| 管理员 `admin` | 管理用户、店铺和授权范围，维护知识，查看受权限约束的管理与审计数据 |

桌面端提供完整操作界面；手机端面向任务查看和主管审批。API 在服务端验证角色和店铺范围。

## 本地快速启动

需要 Python 3.11、Node.js 24、Docker Compose，以及已下载的 `bge-m3` 和 `bge-reranker-v2-m3` 本地模型。以下 PowerShell 命令均在仓库根目录执行。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -c "import subprocess,sys,tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; subprocess.check_call([sys.executable,'-m','pip','install',*p['dependencies'],*p['optional-dependencies']['test']])"
npm --prefix frontend ci
if (!(Test-Path .env.local)) { Copy-Item .env.example .env.local }
docker compose -p e-commerce_operations up -d
```

在仅本机保存的 `.env.local` 或进程环境中设置以下配置，勿将真实值提交到 Git：

| 配置 | 用途 |
|---|---|
| `JWT_SECRET_KEY` | 自行生成的随机签名密钥；各业务进程使用相同配置 |
| `DATABASE_URL`、`LANGGRAPH_DATABASE_URL` | 分别使用 asyncpg、psycopg 驱动，并指向同一业务数据库 |
| `KNOWLEDGE_EMBEDDING_MODEL_PATH`、`KNOWLEDGE_RERANKER_MODEL_PATH` | 两个已安装模型的本地目录 |
| `MILVUS_URI`、`MILVUS_COLLECTION`、`KNOWLEDGE_UPLOAD_DIR` | 知识服务地址、集合及本地上传目录 |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` | 仅在主动使用真实模型时配置；阶段十批准模型为 `deepseek-v4-flash` |
| `PLATFORM_BASE_URL`、`PLATFORM_CLIENT_ID`、`PLATFORM_CLIENT_SECRET`、`PLATFORM_WEBHOOK_SECRET` | 本地契约模拟器及 Webhook 配置 |

Compose 提供的开发端口为 PostgreSQL `5434` 和 Milvus `19530`。已有服务应复用原 Compose 项目，不要为了启动应用删除共享数据卷。

```powershell
python -m alembic upgrade head
python -m scripts.seed_demo
npm --prefix frontend run build
python -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000/app/`。演示账号由 [seed](backend/seed.py) 初始化，演示口令也由该文件定义。开发界面可另开终端运行 `npm --prefix frontend run dev`，其业务 API 代理指向本机 `8000` 端口。

需要后台执行任务时，在仓库根目录分别启动对应进程，并使用一致的配置：

```powershell
python -m scripts.run_analysis_worker
python -m scripts.run_knowledge_worker
python -m scripts.run_optimization_worker
python -m scripts.run_manual_review_worker
```

平台投递另开终端，并仅在该进程中开启 `RUN_PHASE10_PLATFORM_DELIVERY=1` 后运行 `python -m scripts.run_platform_delivery_worker`。没有平台配置时，本地发布事实与外部投递结果仍分别保存。

手动演示平台投递时，另开终端运行下列本机测试模拟器。先在该终端环境中配置 `PLATFORM_SIMULATOR_CLIENT_ID` 和 `PLATFORM_SIMULATOR_CLIENT_SECRET`，与投递 Worker 的对应配置保持一致；Worker 的 `PLATFORM_BASE_URL` 指向 `http://127.0.0.1:8766`。

```powershell
python -m uvicorn tests.support.platform_simulator:app --host 127.0.0.1 --port 8766
```

## 演示流程

1. 管理员上传[合成知识文件](tests/fixtures/phase10-knowledge.md)，类别填写“通用规则”，等待版本激活；业务检索只使用对应商品类别和通用规则。
2. 运营选择授权店铺和日期范围，发起分析，再从候选商品创建优化方案。
3. 查看可信事实、文案差异、合规结论和知识引用；提交主管审批。
4. 需要人工修改时，由主管退回修改，运营保存新修订并等待人工复核 Worker 完成，再次提交审批。
5. 主管批准后查看本地发布记录和独立的平台投递状态。异步投递完成后可刷新详情页。
6. 在调用记录和审计页面核对安全元数据，不查看或导出原始模型请求。

## 测试与验收

普通回归保持所有真实服务 opt-in 开关未设置，并移除测试进程中的真实模型密钥：

首次运行浏览器测试前，在 `frontend` 目录执行 `npx playwright install chromium` 安装 Chromium。若需指定浏览器缓存目录，先设置 `PLAYWRIGHT_BROWSERS_PATH`，安装与运行测试时使用同一值；`npm ci` 本身不安装浏览器。

```powershell
python -m pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

真实 PostgreSQL 验证单独设置 `RUN_POSTGRES_INTEGRATION=1`，运行 `tests/test_phase10_postgres.py` 等集成测试，结束后清除开关。真实本机 RAG 验证使用 `RUN_KNOWLEDGE_INTEGRATION=1`；联合运行以下两个文件时同时启用 PostgreSQL 测试连接池隔离：

```powershell
$env:RUN_KNOWLEDGE_INTEGRATION='1'
$env:RUN_POSTGRES_INTEGRATION='1'
try {
  python -m pytest tests/test_knowledge_integration.py tests/test_optimization_rag.py -q
} finally {
  Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION,Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
}
```

真实服务 E2E 的唯一入口为 `python scripts/run_phase10_e2e.py`。这是已准备好的本机验收环境入口：[启动器](scripts/run_phase10_e2e.py) 中的 `PYTHON`、`MODEL_ROOT`、`EVIDENCE_ROOT` 和 `PLAYWRIGHT_BROWSERS_PATH` 使用固定本机位置，不继承上面的虚拟环境或模型路径设置。在其他机器复现前，须按这些常量准备运行环境，或将其调整到该机器已安装的运行时、模型、证据目录和浏览器位置。

启动器使用独立数据库、Milvus 集合、本地模型和确定性模型 HTTP 服务；浏览器不拦截业务 API。完整运行、证据和清理均由启动器管理。

真实 DeepSeek 门禁为 `tests/test_phase10_deepseek.py`，仅在 `RUN_PHASE10_DEEPSEEK=1` 时调用外网。每个 Agent 最多一次主调用和一次 schema 修复；失败需保留证据并排查，不能自动反复调用直至通过。实际验收结果见[第十阶段验证报告](docs/phase10-validation-report.md)。

## 安全配置

密钥、令牌和连接凭据只放在本机安全配置或进程环境中。不要把 `.env.local`、原始请求、模型输出、HTTP 授权头、浏览器 trace 或运行日志提交到 Git。

Webhook 校验事件 ID、时间戳与原始请求体的联合签名，并保存摘要和幂等接收事实。浏览器不能配置平台凭据、重试次数或故障模式。

## 已知限制

- 当前平台适配是本地契约模拟，不能据此声称真实商家平台已认证或已联调。
- 真实模型具有非确定性；离线故障测试、真实模型验证和确定性服务 E2E 的结果分别记录。
- 平台投递无手工重试按钮，终态失败通过安全错误码和审计诊断；详情页需要刷新查看异步变化。
- 本地服务、模型文件和浏览器运行环境需提前准备。Compose 的开发配置不是生产部署配置。

## 后续可选工作

在单独授权和设计后，可部署到指定服务器，或在取得真实商家权限后实现某个平台的正式认证、逐店 OAuth、密钥托管与 API 适配。这些工作不属于当前阶段。
