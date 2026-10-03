# 门区报修工单与备件子系统（psdline.maintain）

纸质流程时代「工单开没开、备件领了多少、门锁没锁」三张皮对不上。本子系统用一个
SQLite 库把**工单状态、备件库存、门的维修锁**放进同一批事务，并保留三张
**只追加、物理禁改禁删**的历史表。运行期零第三方依赖（仅标准库 `sqlite3`）。

## 1. 关键设计取舍

### 1.1 备件：开单预占 + 领料实扣（混合账）

两种方案都成立，本系统选「预占 + 实扣」：

| 方案 | 优点 | 问题 |
|---|---|---|
| 修完再实扣 | 简单 | 跨班/多单并发时件可能被别的工单先领走，到领料才发现没件 |
| 开单即预占（**本方案**） | 计划件被冻结，不被抢；可用量随时可信 | 长期挂单会冻住库存 → 用完工/关单/废单自动释放兜底 |

账分两层：

- `parts.on_hand`：**实物结存**（只有入库/发料/退料/盘点调整会动它）；
- `parts.reserved_qty`：**预占量**（开单加、领料减、完工/关单/废单释放未领部分）；
- 可领量 = `on_hand - reserved_qty`。

### 1.2 废单：未领自动释放，已领必须人工退料

- 未领用的预占：**自动释放**（件本来就在库，只是冻结，无账实风险）；
- 已领用的件：**不自动回补**，按「已领未退」数量生成退料任务。件可能已被用掉、
  损坏或丢失，自动回补等于造一笔假库存。库管点收实物后逐笔（可分批）确认，
  库存才增加；找不回的部分留在任务上，必须走盘点调整流程，系统不会默默吞掉。

### 1.3 门的锁定

- 开单即上「维修锁」，同一樘门同时**只允许一张活动工单**——由部分唯一索引
  `uq_door_active_wo` 从存储层强制，不靠应用加锁；
- 完工**不解锁**（修完还要验收），只有**关单或废单**才解锁。

### 1.4 工单历史不可变

`wo_events`（工单事件流）、`inventory_ledger`（库存流水）、`audit_entries`
（管理审计）、`reminders`（提醒记录）四张表只追加，SQLite 触发器在数据库层面
拒绝任何 UPDATE/DELETE（直接改库也会被拦）。`work_orders`/`parts`/`doors`
只是可重放的当前状态投影；库存真相永远等于流水带正负号的求和。

## 2. 工单状态机

```
                 开单(预占+上锁)
   ┌──────────────────────────────────┐
   │                                  ▼
 ┌──────┐ start   ┌─────────────┐ complete ┌──────────────┐
 │ open │ ──────▶ │ in_progress │ ───────▶ │ repair_done  │
 └──┬───┘         └──────┬──────┘          └──────┬───────┘
    │  关单(免修)         │ 关单(带件关闭)          │ 关单(验收)
    └────────────────────┴───────────────────────┴────────────▶ closed(解锁)
    │ 管理员废单（任意活动状态）                                   │
    └──────────────────────────────────────────────────────────▶ void(解锁)
                         已领件 → 退料任务(人工)
```

跨班交接由管理员 `reassign` 完成（只改活动单），交接双方、班次、备注进事件流。

## 3. 权限模型

| 动作 | reporter | repair | storekeeper | manager |
|---|:-:|:-:|:-:|:-:|
| 报修开单 | ✓ | ✓ | ✓ | ✓ |
| 领料发料 |   |   | ✓ | ✓ |
| 完工/关单 |   | 负责人本人 |   | ✓ |
| 跨班改派 / 废单 |   |   |   | ✓ |
| 入库 / 退料确认 |   |   | ✓ | ✓ |
| 盘点发起 |   |   | ✓ | ✓ |
| 盘点批准（调账） |   |   |   | ✓ |
| 用户/门/备件建档 |   |   |   | ✓ |

关单权限刻意不给报修人（不能自己开自己关）；盘点必须库管发起、管理员批准，
同一个人无法把盘点差异直接调成账面数字。HTTP 接口通过 `X-User-Id`
请求头（或 body 的 `actor` 字段）鉴权，动作接口匿名一律 `401`。

## 4. 并发与一致性

- 每个写事务以 `BEGIN IMMEDIATE` 开始，进事务即持有库级写锁，「先读后写」的
  超卖竞态在锁等待层串行化；多备件按 SKU 固定顺序加锁，杜绝死锁；
- 多件领料是**一个事务**：任一件超领/实物不足，整笔回滚，不会出现「A 扣了
  B 没扣」；
- 数量列全部 `CHECK (>= 0)`、`reserved_qty <= on_hand`、
  `issued_qty <= planned_qty`，越界在存储层兜底；
- `GET /api/maintain/verify` 核对流水重算结存 vs 投影、门锁定 vs 活动工单等
  全部镜像关系，只报告不自动改账；
- 发现漂移（例如绕过系统动了文件）：`POST /api/maintain/rebuild` 由管理员从
  只追加流水重建可推导投影（实物=流水和，预占=活动单备件行和，门锁=活动单），
  重建动作本身写审计；无法从历史推导的实物丢失要走双人盘点。

## 5. 超期提醒

`POST /api/maintain/scan-overdue`（建议挂定时任务，如每 30 分钟）扫描活动工单：

- `due`：超过 SLA（默认 24h）未完工 → 提醒负责人；
- `done_unclosed`：完工后超过升级窗口（默认 24h）仍未关单（门还锁着）→ 提醒负责人；
- 再经过一个升级窗口仍未闭环 → 升级提醒管理员；
- 去重粒度为「工单 + 级别 + 情形 + 目标人」：改派后新负责人会收到本级提醒，
  同一人不会被重复打扰；提醒落只追加表，外部通知（`Notifier` 接口，可接
  短信/企业微信/邮件）在事务外投递，通知失败不回滚业务事实。

## 6. 运行

```bash
# 完整业务流程演示（含并发领料、权限拦截、两级提醒、废单退料、盘点、核对）
python3 -m psdline.maintain demo

# 启动服务（默认 var/state/maintain.db，端口 8090）
python3 -m psdline.maintain serve
python3 -m psdline.maintain scan      # cron：超期扫描
python3 -m psdline.maintain verify    # cron/巡检：账实核对，有偏差退出码 2
```

首次启动自动种入四个账号（`u-report` 王报修、`u-repair` 李维修、
`u-store` 赵库管、`u-admin` 陈管理）、三樘门和五种备件。

接口一览（动作均为 POST，查询为 GET，前缀 `/api/maintain`）：

```
POST /users /doors /parts /receive
POST /work-orders                         开单（body: door_id,title,owner_id,parts:[{sku,qty}],sla_hours,shift）
GET  /work-orders?status=open             列表
GET  /work-orders/{id}                    详情（含备件行与不可变事件流）
POST /work-orders/{id}/issue              领料（items:[{sku,qty}]）
POST /work-orders/{id}/start|complete|close|reassign|void
POST /return-tasks/{id}/confirm           人工退料（qty 可空=全退）
POST /stocktakes   /stocktakes/{id}/decide 盘点（approve:true/false）
POST /scan-overdue  GET /reminders
GET  /verify  POST /rebuild  GET /ledger  GET /audit
```

## 7. 测试

39 个用例覆盖：预占/实扣、并发领料不超卖、入库与发料并发、多行部分失败回滚、
四门状态机、关单权限、废单人工退料（分批/超退）、两级提醒与去重/改派、
盘点双人控制、账实漂移检测与投影恢复、四张历史表禁改禁删、HTTP 鉴权与状态码、
重启恢复。并发用例使用文件库（内存单连接测不出多连接的写锁串行化）。

```bash
python tools/run_tests.py tests/test_maintain.py   # 无 pytest 的离线环境
pytest tests/test_maintain.py                      # 正常 CI
```
