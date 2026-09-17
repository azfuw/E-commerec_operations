# 物流后端交付与验证

2026-09-17，分支 `codex/logistics-command-center`。复用现有 FastAPI、SQLAlchemy、JWT、User/Store/Order 及店铺权限，不新增依赖。物流事实保存在独立表中，不修改原订单、退款或库存事实。

## 已交付

- `backend/logistics_models.py`：Shipment、ReturnCase、ShipmentEvent、ExceptionTask、AgentRun 五张持久化表。事件的 UPDATE/DELETE 由 SQLite/PostgreSQL 触发器禁止；物流、退货、任务使用版本检查防止并发覆盖。
- `backend/logistics.py`：带鉴权的仪表盘、筛选分页、详情轨迹、建单、发货/运输/签收、退货申请/审核/寄回/入仓/质检结案、异常指派和处理、规则巡检、运行记录、可追溯查询与日报接口。
- `backend/logistics_seed.py`：每店稳定选择 12 个现有订单，生成相对日期场景及首轮异常任务。重启不重置已修改状态；演示标记明确。20 单的启动种子会保留 8 单供人工创建物流记录。
- `alembic/versions/0008_logistics_department.py`：接续 0007 的迁移；有持久事实时拒绝降级删除物流表。
- 接口字段、权限、状态流转与集成方法见 `docs/logistics-api-contract.md`。

规则覆盖待发货超时、在途 48 小时无新扫描、预计到货超时、退回超时和退货入仓后 24 小时未完成质检。异常携带订单、运单、观察时间及触发节点证据；同一证据不会因重复或并发巡检重复生成，已人工处理记录不重开，后续新停滞节点可产生新任务。

巡检在分析前重新读取事实：PostgreSQL 按稳定顺序锁物流单及退货记录；SQLite 先取得写锁。测试曾复现 Worker 读取待发货快照后订单已发货、旧快照仍误建异常的问题；修复后该竞态不再产生假任务。

## 验证证据

运行环境：`D:\E-commerce_operations_env\python.exe`，Python 3.11；缓存、临时目录和 pytest 目录均在 D 盘。

```powershell
$env:PYTHONPYCACHEPREFIX='D:\E-commerce_operations_runtime\pycache'
$env:TEMP='D:\E-commerce_operations_runtime\temp'
$env:TMP=$env:TEMP
& 'D:\E-commerce_operations_env\python.exe' -m pytest `
  tests/test_logistics.py tests/test_logistics_migration.py `
  -q --tb=short --basetemp 'D:\E-commerce_operations_runtime\pytest-logistics'
```

结果：**14 passed in 2.43s**。覆盖内容包括：

- 真实接口的建单、查询、发货、签收、完整退货过程及终态拒绝。
- 带时区时间输入、UTC 归一化、时间倒序/未来时间拒绝、终态非空约束。
- 未登录拒绝、跨店详情和写入拒绝、跨店指派拒绝、停用店铺隔离、Agent 引用和运行聚合隔离。
- 相同巡检的幂等性；两个独立 SQLite 会话并发仅建一个异常与一个发现事件；旧版本修改失败；Worker 旧快照不产生假异常。
- SQL 直接改删历史事件被数据库拒绝。
- 种子重复执行不增加物流记录，不修改原订单事实；演示覆盖所有四类规则与五个退货状态。
- 物流简报按汇总意图回答，支持“退单”别称；引用正文为中文状态和北京时间。
- 在空的 SQLite 数据库执行 0008 升级后，模型元数据比较无漂移；空库降级成功；PostgreSQL 离线迁移 SQL 可编译并含不可变历史触发器。

独立审查代理再次运行旧快照和并发去重测试通过，并独立核对 SQLite 迁移列、可空性、CHECK 约束及空库降级。主应用接线、启动器、定时 Worker、旧功能回归和浏览器操作由主任务汇总验证。

### 实际 PostgreSQL 定向核验

另在本机 `127.0.0.1:5434` 创建一个随机后缀、仅本次拥有的 `ecommerce_logistics_probe_*` 临时数据库，实际执行全部迁移到 0008，再执行一次有界业务核验。未读写原 `ecommerce` 库业务数据。核验结束后释放应用连接池，使用已创建库的精确标识符删除临时库，并确认其已不存在。

```powershell
& 'D:\E-commerce_operations_env\python.exe' `
  'D:\E-commerce_operations\data\logistics-demo\probe_postgres.py'
```

退出码 0，结果如下：

```json
{
  "status": "passed",
  "cleanup": "confirmed",
  "seeded_shipments": 36,
  "stale_snapshot_false_exceptions": 0,
  "concurrent_tasks": 1,
  "concurrent_detection_events": 1,
  "return_final_state": "closed",
  "return_events": 5,
  "original_order_unchanged": true,
  "append_only_trigger": "rejected_update"
}
```

该核验使用新建人工物流单，避免种子异常的去重键掩盖错误：独立会话先读待发货快照，另一会话实际发货并提交，巡检重读后没有创建假异常；两会话同时巡检另一新超时单，只生成一个异常和一条发现事件。实际 PostgreSQL 上完成退货审核、寄回、入仓、质检结案，保留五条退货事件，原订单事实未变；直接 SQL 篡改历史事件被触发器拒绝。

这是一次真实 PostgreSQL 的定向业务与锁验证，**不等同于已执行项目全部 PostgreSQL 集成测试**。主任务另已执行临时库的升级、无漂移检查、降到 0007、再升级与无漂移检查。

## 真实运行边界

规则 Agent 是确定性规则及查询意图路由，响应标明 `deterministic_rules`；本实现未调用大模型、真实快递接口或付费外部服务。轨迹来自种子或人工登记，物流结案不会执行退款。所有聚合及证据仅来源于当前权限内的持久记录。

本次后端测试执行 SQLite，并补充了上述真实 PostgreSQL 迁移、行锁、并发去重、业务流转及不可变事件核验。正式部署前仍应执行目标环境对应的完整 PostgreSQL 集成门禁。

当前规模使用单体应用内规则扫描；仪表盘和规则扫描在内存聚合权限内订单，大规模生产组合应改为数据库聚合/分批处理。SQLite 演示只有一个写入者，适合单机低并发；生产沿用 PostgreSQL。
