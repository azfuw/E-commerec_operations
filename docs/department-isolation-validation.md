# 部门权限隔离验证

日期：2026-09-21。分支：`codex/logistics-command-center`。本次仅修正权限隔离及其界面、迁移和验证；由 Astra max 主导后端与独立安全审查，Sol high 完成前端修改。

## 权限规则

| 账号 | 运营业务 | 物流业务 | 系统管理 |
| --- | --- | --- | --- |
| 运营员工或主管 | 按原有角色、店铺权限 | 拒绝 | 拒绝 |
| 物流员工或主管 | 拒绝 | 按店铺权限 | 拒绝 |
| 管理员 | 可访问，保留动作级校验 | 可访问 | 可管理部门、角色和店铺授权 |

`User.department` 明确区分 `operations` 与 `logistics`。普通账号不能自行改部门，用户名不决定部门。所有运营业务、知识库、审计及 Agent 评测接口要求运营权限；全部物流接口要求物流权限。登录、当前身份和授权店铺列表共用；平台回调继续使用独立签名验证。

前端按部门选择登录落点、显示导航并在挂载页面前阻止跨部门直达。修改网址、请求参数或绕过页面直接调用接口，均不能绕过后端权限。JWT 只标识身份，后续授权读取数据库当前状态。

管理员可在“系统管理 → 编辑用户 → 所属部门”调整账号部门，变更前后值写入审计。物流异常只能指派给有对应店铺权限的有效物流员工或管理员。后台分析、优化、人工审查、知识索引、物流巡检和平台发布分别复核执行权限与结果写入权限。

## 已执行的验证

| 验证 | 实际结果 |
| --- | --- |
| 后端完整离线回归 `python -m pytest -q` | 1145 passed，48 skipped，exit 0；235.27 秒 |
| 前端 Vitest | 17 个文件，101 passed |
| TypeScript 与生产构建 | 通过；保留原主应用分包体积提示 |
| 原运营与管理浏览器回归 | 9 passed |
| 真实服务浏览器验收（无 API mock） | 7 passed；8011 独立演示库 |
| 部门管理页面补充截图验证 | 1 passed |
| SQLite 迁移与 PostgreSQL 编译、真实迁移验证 | 3 passed |
| PostgreSQL 并发授权回归 | 2 passed；续租/停用无死锁，真实撤权等待授权事务释放 |
| 8010 四种真实登录账号验证 | `logistics`、`warehouse`：运营 403 / 物流 200；`operations`：运营 200 / 物流 403；`admin`：两者 200 |
| `git diff --check` | 通过 |

完整后端日志保存在 `data/logistics-demo/department-backend-tests.log`。48 个跳过项为需要明确启用的 PostgreSQL、外部模型等集成检查；本次另行启用了部门迁移与并发相关 PostgreSQL 验证。仅有一条既有 LangChain 依赖弃用提示。最终代码已在 8010 服务启动，健康检查、前端资源和四类账号权限矩阵复验通过。

真实浏览器覆盖新建运单、物流节点、完整退货结案、异常分配、巡检、手机布局、仓库店铺范围、管理员跨部门切换、双向页面/接口拒绝，以及管理员从页面修改部门后旧登录凭证立即生效。测试期间改动的演示账号部门已在 `finally` 中还原。

## 独立审查与修正

独立 Astra max 审查发现并关闭了两项问题：

1. **P1：平台认证期间撤权仍可能发出后续发布请求。** 发布客户端现在在每次实际 PUT 前调用数据库授权复核，覆盖初次 OAuth 和 401 后重新认证。使用真实客户端配合 MockTransport，分别在两次认证等待阶段撤销部门权限；不再发出越权 PUT。独立相关回归 50 passed。
2. **P2：知识索引续租和停用操作的锁顺序可能形成死锁。** 授权读取改用 PostgreSQL `FOR SHARE`。独立临时 schema 中复现原 `40P01` 后，复测续租和停用均完成；真正修改部门/角色的 UPDATE 仍等待授权事务结束，后续权限检查返回 403。

复查未发现未处理的 P1/P2 问题。审查同时覆盖前端直达路径、后台执行和写入边界、异常指派、既有登录凭证、迁移及管理员回收权限。平台请求验证使用本地模拟传输；本次没有调用付费模型或真实承运商、电商平台业务接口。

## 数据保全与本机迁移

- 原 SQLite 演示库升级前已备份到 `data/logistics-demo/backups/pre-department-20260921-200822.sqlite`。
- 逐条核对：原有 60 个订单、36 个运单、15 条退货、180 条物流事件的全部字段不变；原有账号密码、角色、状态、创建时间不变。自动巡检可以继续追加新事件。
- 旧 SQLite 库仅在首次补齐部门列时，将启动脚本生成的确定性 `logistics`、`warehouse` 身份归入物流；同名但非该脚本生成的账号不自动改部门。之后的人工部门配置在重启时保留。
- 本机 PostgreSQL 升级前已备份到 `data/logistics-demo/backups/postgres-pre-department-20260921-201113.dump`，随后从 `0007` 升至 `0009`。原有用户 4、订单 415、商品 301、工作流 5，升级前后数量一致；既有用户默认归属运营。
- `alembic check` 返回 `No new upgrade operations detected`。另在事务内创建独立 PostgreSQL schema，验证完整迁移、旧用户与物流记录保留、非法部门约束、降级再升级和模型一致性；验证后事务回滚，不遗留测试数据。

本机展示服务位于 `http://127.0.0.1:8010/app/`。演示账号 `logistics`、`warehouse`、`operations`、`admin` 使用同一演示密码 `Logistics!2026`；普通部署不会自动生成这些账号。

## 界面证据

- [物流账号手机界面](assets/logistics/logistics-department-isolation.png)
- [运营账号工作台](assets/logistics/operations-department-isolation.png)
- [管理员部门管理](assets/logistics/department-management.png)
- [管理员共享管理页](assets/logistics/shared-admin.png)

使用说明见 [物流指南](logistics-guide.md)，接口约束见 [物流 API](logistics-api-contract.md)。
