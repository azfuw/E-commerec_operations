# 物流工作台交付验证

2026-09-21 权限修正与最新验收见 [部门隔离验证报告](department-isolation-validation.md)。以下保留 2026-09-17 的交付记录；当前普通账号仅能进入所属部门，跨部门切换仅向管理员提供。

日期：2026-09-17。代码基于原项目 `711ca05`，开发分支 `codex/logistics-command-center`。Astra max 主导业务设计和复杂后端实现，独立 Astra max 审查；Sol high 完成界面，Terra high 验证原有回归。

## 可体验结果

- 本机入口：http://127.0.0.1:8010/app/logistics
- 启动：仓库根目录执行 `./start-logistics.ps1`。
- 演示账户：`logistics / Logistics!2026`，三个店铺主管；`warehouse / Logistics!2026`，仅旗舰店。
- 初始演示：36 票运单、15 条退货流程、18 条异常任务；状态相对首次启动时间生成，不会每次启动重置。
- 演示后台每 60 秒自动巡检；正常部署可运行独立的物流 Worker。关闭异常不会重复创建同一证据的任务。

## 最终自动化验证

| 验证 | 结果 |
| --- | --- |
| 后端完整离线回归 `python -m pytest -q` | **1106 passed，45 skipped**；1 条既有 LangGraph 依赖弃用提示 |
| 前端完整单元测试 `npm test -- --run` | **16 文件，88 passed** |
| TypeScript 与生产构建 `npm run build` | **通过**；原主应用分包仍有体积提示，物流页面独立懒加载 |
| 真实服务 Playwright `playwright test -c playwright.logistics.config.ts` | **4 passed**，无 API mock |
| 原运营与管理页面 Playwright `playwright test -c playwright.config.ts` | **9 passed**，使用现有 API fixture 验证运营流程及权限 |
| PostgreSQL 迁移 | 独立临时数据库执行 upgrade head → check → downgrade 0007 → upgrade head → check，全部通过，未发现结构漂移 |
| PostgreSQL 真实业务定向核验 | 两会话旧快照无误报、并发巡检仅 1 任务/1 事件、完整退货结案、历史事件防篡改均通过；自有验证库已清理 |
| 代码检查 | `git diff --check` 通过 |

45 个跳过项为原项目需要明确启用的外部模型、RAG、PostgreSQL 等集成门禁。未调用真实付费模型或真实承运商接口。迁移验证使用本机现有 PostgreSQL 服务的独立数据库，完成后只删除该次创建的验证库；未修改原电商数据库。

## 真实浏览器覆盖

1. 登录自动返回物流入口、总览、规则巡检、含运单引用的物流简报、异常分配和解决；390px 手机布局无页面横向溢出。
2. 通过页面新建运单、登记实际发货、立即申请退货、审核、登记退回运输、收货、质检结案及 CSV 下载。验证了此前时间精度和退货字段传递问题的真实修复。
3. 未登录接口拒绝访问；仓库员工只能读取自己的店铺；不能读取其他店铺数据。
4. 运营与物流共用外框；部门切换保留最近业务页；共享审计页面保留来源部门；物流子页面刷新、后退与高亮一致；手机部门切换与末项导航可用。

浏览器验证独立运行在 8011 端口，数据库位于 `data/logistics-demo/e2e/`，不污染 8010 展示库。数据和签名密钥在被 Git 忽略的本机目录中，未纳入交付代码。

## 已修复并复验的问题

- 并发时巡检读取旧运单快照造成错误异常：在一致的锁顺序下刷新运单和退货事实，保留唯一证据去重。
- 退货收货和结案时错误重复发送退回运单字段：只在寄回节点发送。
- 新退货立即审核时默认时间被截断到分钟：保留秒，并在默认时间未修改时使用提交时刻。
- 新增启动器测试污染环境变量影响原回归：测试完整恢复所有被修改的配置，完整回归已重新通过。
- 手机导航继承桌面按钮整行宽度：手机端改为内容宽度，并用浏览器检查第二个导航入口在屏幕内可见。

## 截图与说明

- [桌面总览](assets/logistics/desktop-overview.png)
- [运单详情](assets/logistics/shipment-detail.png)
- [Agent 事实引用](assets/logistics/agent-evidence.png)
- [手机总览](assets/logistics/mobile-overview.png)
- [运营工作台](assets/logistics/operations-workbench.png)
- [共享审计页面](assets/logistics/shared-audit.png)
- [手机运营工作台](assets/logistics/mobile-operations.png)
- [使用指南](logistics-guide.md)
- [API 契约](logistics-api-contract.md)
- [独立审查报告](logistics-review-report.md)

## 部门导航与样式优化

2026-09-17 根据工作台切换反馈，运营与物流统一使用“智营台”品牌、固定部门切换、账号栏和响应式外框。删除物流页重复的侧栏及“返回运营工作台”按钮；业务菜单与“管理与支持”分组。物流视图使用白名单 `view` 查询参数，支持登录回跳、刷新和浏览器历史。

样式职责：`frontend/src/styles.css` 管理公共主题、字体和通用页面样式；`AppShell.vue` 的 scoped 样式管理共享导航与外框；各业务页面只管理自己的表格、筛选、看板和弹窗。物流两段叠加样式已合并，并改用公共颜色变量；运营空状态与审计筛选面板同步调整。独立前端审查未发现重要问题。本轮没有修改后端。

## 当前范围

这是可运行、可持久化的内部物流工作台。Agent 为可解释的确定性规则和受限业务问答，当前未接外部大模型或承运商实时接口；退货结案不会执行财务退款。当前一个订单一个运单、一票运单一次退货流程。总览按全部当前可见运单统计，准时率以已有签收记录为分母。
