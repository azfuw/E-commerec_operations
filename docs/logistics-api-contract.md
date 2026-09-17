# 物流部门 API

前缀 `/logistics`，沿用现有登录及 Bearer JWT。所有读取、巡检和修改均按当前有效用户的有效店铺权限过滤；管理者的管理员角色可访问全部有效店铺。越权详情统一 404，显式选择无权限店铺返回 403，停用店铺返回 404。写入请求拒绝未定义字段，错误响应沿用 FastAPI `detail`。

结构化日期统一 ISO 8601 UTC，实际节点输入必须带时区，不能倒序或晚于服务器时间（允许 5 秒钟差）。自然语言证据使用明确标注的北京时间；今日签收按 Asia/Shanghai 自然日计算。没有已签收样本时准时率为 `null`。

## 查询接口

| 方法与路径 | 参数 | 返回 |
| --- | --- | --- |
| GET `/dashboard` | `store_id?` | 仪表盘对象 |
| GET `/shipments` | `store_id?`, `status?`, `carrier?`, `q?`, `risk_only=false`, `page=1`, `page_size=20` | 分页 Shipment |
| GET `/shipments/{id}` | — | Shipment + `events`, `exceptions`, `return_case` |
| GET `/orders` | `store_id?`, `q?`, `limit=100`（上限 500） | 未建立物流单、未取消的订单数组 |
| GET `/returns` | `store_id?`, `status?`, `q?`, `page`, `page_size` | 分页 Return |
| GET `/exceptions` | `store_id?`, `status?`, `kind?`, `page`, `page_size` | 分页 Exception |
| GET `/assignees` | 必填 `store_id` | `[{id,username}]`，仅有效且能访问该店的用户 |
| GET `/agent/runs` | `store_id?`, `limit=20`（上限 100） | Run 数组，最近在前 |
| GET `/agent/brief` | `store_id?` | AgentAnswer |

分页格式为 `{items,total,page,page_size}`，`page_size` 1–100。`q` 匹配物流单的订单号、运单号、目的地、承运商、店铺名，退货查询匹配订单号、正向/退货运单号和退货原因。`risk_only` 按实时规则判断，与异常是否已人工结案分开。

Shipment 字段：

```text
id, order_id, order_number, store_id, store_name, carrier, tracking_no,
status: pending_dispatch | in_transit | delivered,
destination, dispatch_due_at, expected_delivery_at, dispatched_at?, delivered_at?,
last_event_at?, last_location, risk_level: normal | medium | high,
issue_codes: string[], open_exception_count, created_at, is_demo
```

`order_number` 当前使用现有 Order 的 `id`；没有另造平台订单号。一个订单一个正向物流单，承运商与运单号组合唯一。

Return 字段：

```text
id, shipment_id, order_id, order_number, store_id, store_name,
status: requested | approved | in_transit | received | closed | rejected,
reason, carrier, tracking_no, requested_at, expected_return_at, received_at?,
closed_at?, updated_at, is_demo, shipment_tracking_no, destination
```

Event 字段：

```text
id, shipment_id, return_id?, event_type, occurred_at, location, description,
actor_name, source: manual | demo | agent, created_at
```

事件按登记时间排序，同时保留实际发生时间。发货、运输、签收、退货推进、异常发现、指派及处理都会追加事件；数据库触发器禁止更新与删除历史事件。

Exception 字段：

```text
id, shipment_id, return_id?, order_number, store_id, store_name,
kind: dispatch_overdue | no_movement | delivery_overdue | return_overdue,
severity: medium | high, status: open | in_progress | resolved,
title, description, recommended_action, evidence,
assignee_id?, assignee_name?, created_at, resolved_at?, resolution
```

`evidence` 保存 shipment_id、order_id、tracking_no、status、observed_at、trigger_at、threshold_hours、return_id；退货风险另有 return_status。

仪表盘：

```text
as_of,
metrics: {total_shipments,pending_dispatch,in_transit,delivered_today,
          open_exceptions,active_returns,on_time_rate},
status_distribution: [{status,count}],
carriers: [{carrier,total,delivered,on_time_rate}],
recent_exceptions: Exception[] (最近/优先 6 条),
recent_events: Event[] (最近 8 条),
agent: {mode:'deterministic_rules',label:'规则巡检 Agent',carrier_connected:false,
        last_run_at?,last_run_status?},
data_mode: demo | operational | mixed | empty
```

## 写入接口与状态流转

| 方法与路径 | JSON 请求 | 成功返回 |
| --- | --- | --- |
| POST `/shipments` | `{order_id,carrier,tracking_no,destination,dispatch_due_at,expected_delivery_at}` | 201，物流详情 |
| POST `/shipments/{id}/events` | `{action:'dispatch'\|'transit'\|'deliver',occurred_at,location,description}` | 200，物流详情 |
| POST `/returns` | `{shipment_id,reason,expected_return_at}` | 201，Return |
| POST `/returns/{id}/transition` | `{status,occurred_at,carrier?,tracking_no?,note}` | 200，Return |
| PATCH `/exceptions/{id}` | `{action:'assign',assignee_id}` 或 `{action:'resolve',resolution}` | 200，Exception |
| POST `/agent/patrol` | `{store_id?}` | 200，Run |
| POST `/agent/query` | `{question,store_id?}` | 200，AgentAnswer |

正向：`pending_dispatch → in_transit → delivered`，在途允许追加 `transit` 节点，签收后不允许修改正向状态。发货截止不得早于下单时间，预计到货不得早于发货截止，实际节点不得早于下单/上一实际物流节点。补录历史节点允许早于系统登记时间。

退货：仅已发货或已签收订单可申请，每单一个退货记录。`requested → approved → in_transit → received → closed`，或 `requested → rejected`。寄回必填承运商及运单号；每步必须填写说明；结案说明作为质检记录追加历史。`closed`/`rejected` 不可回退。物流结案不会修改原订单、退款或库存数据，也不执行真实退款。

异常：`open → in_progress → resolved`，也可直接处理 `open → resolved`。指派人必须有效且拥有该店铺权限；处理必须写明结果；已处理任务不允许再改。每次操作保留当前操作者及时间。

非法状态流转、重复物流单/退货单和并发更新冲突返回 409，时间或业务输入不合法返回 422。Shipment、Return、Exception 均使用乐观版本检查；PostgreSQL 的修改/巡检统一按物流单、退货或任务顺序取行锁。

## 规则 Agent 的实际能力

规则巡检运行在当前权限内，只对持久化登记事实做判断，不调用大模型，不连接承运商。规则如下：

| 规则 | 条件 |
| --- | --- |
| 发货超时 | 待发货且超过发货截止 |
| 轨迹停滞 | 运输中且最近实际物流节点超过 48 小时无更新 |
| 到货超时 | 运输中且超过预计到货时间 |
| 退货超时 | 申请/待寄回/寄回运输中超过预计退回时间；或入仓超过 24 小时仍未质检结案 |

判定前重读并锁定物流和退货事实，避免使用 Worker 较早读取的状态。SQLite 在重读前取得写锁；PostgreSQL按稳定顺序取得行锁。

异常唯一键由物流单、规则、退货单和触发节点时间组成，数据库 `ON CONFLICT DO NOTHING` 保证并发巡检不重复生成。已处理的同一证据不会重开；后续新的停滞节点形成不同证据，可产生新任务。规则仍触发但人工已结案时，实时风险标记与待办数量会不同，这是有意保留的事实与处理结果区别。

Run：

```text
id, status:'completed', mode:'deterministic_rules', started_at, completed_at,
scanned_shipments, created_exceptions, existing_exceptions, summary
```

运行记录仅返回完全位于当前店铺范围内的记录；非管理员可见本人和演示初始化的运行。失败不会伪装为完成，当前事务回滚后交由调用 Worker 记录失败。

AgentAnswer：

```text
answer, mode:'deterministic_rules', as_of,
citations:[{shipment_id,order_number,tracking_no,store_name,status,detail}],
suggestions:string[]
```

支持概况/日报/简报、发货超时、到货超时、轨迹停滞、退货/退单进度、完整订单号或运单号查询。回答附权限内订单证据，未覆盖的问题会告知支持范围。证据数组完整返回，不做无提示截断。

## 启动集成

`backend.models` 末尾导入 `logistics_models` 注册同一个 `Base.metadata`；主应用挂载 `backend.logistics.router`。迁移 `0008` 接续 `0007`。

`await seed_logistics_demo(session, now=None)` 在每个有效店铺的稳定最新 12 个非取消订单上建立物流案例，不修改现有订单。已有物流单不重置，重启不增加或回滚已经处理的记录。每店初次种子执行一次规则巡检，创建可追踪的异常任务。

Worker 可调用 `scope_ids(session, actor, store_id)`、`shipment_rows(session, actor, store_id)`，再调用 `patrol(session, actor, rows, ids)`。`patrol` 提交事务并返回 AgentRun，`run_json` 输出 API 格式。生产建议使用现有 PostgreSQL；SQLite 演示只适合单机低写入并发。
