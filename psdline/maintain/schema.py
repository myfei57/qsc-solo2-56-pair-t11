"""建表脚本与装配。

设计要点：

1. ``work_orders``、``parts``、``doors`` 是**当前状态投影**，可以更新；
2. ``wo_events``、``inventory_ledger``、``audit_entries``、``reminders`` 是
   **只追加历史**，由触发器物理禁止 UPDATE / DELETE，满足「工单历史不可变」；
3. 同一樘门同时只允许一张活动工单（open/in_progress/repair_done），用部分唯一索引
   从存储层强制，不靠应用层加锁；
4. 库存数量全部带 ``CHECK (>= 0)``，实物、预占、已领、已退都不可能出现负数；
5. 库存流水带 ``balance_after``，即该备件在这条流水之后的实物结存，配合
   ``parts.on_hand`` 可做「流水重算 vs 投影」的一致性核对。
"""

from __future__ import annotations

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 用户与角色 ----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('reporter','repair','storekeeper','manager')),
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at  REAL NOT NULL,
    created_by  TEXT REFERENCES users(id)
);

-- 门区（设备台账里需要维修的门） --------------------------------------------

CREATE TABLE IF NOT EXISTS doors (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    location    TEXT NOT NULL DEFAULT '',
    locked      INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    active_wo   TEXT,
    created_at  REAL NOT NULL
);

-- 备件台账（当前状态投影） ----------------------------------------------------

CREATE TABLE IF NOT EXISTS parts (
    id            TEXT PRIMARY KEY,
    sku           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    unit          TEXT NOT NULL DEFAULT '件',
    on_hand       INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    reserved_qty  INTEGER NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
    created_at    REAL NOT NULL,
    CHECK (reserved_qty <= on_hand)
);

-- 工单（当前状态投影，完整真相在 wo_events） ---------------------------------

CREATE TABLE IF NOT EXISTS work_orders (
    id            TEXT PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    door_id       TEXT NOT NULL REFERENCES doors(id),
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL CHECK (status IN
                    ('open','in_progress','repair_done','closed','void')),
    reported_by   TEXT NOT NULL REFERENCES users(id),
    owner_id      TEXT NOT NULL REFERENCES users(id),
    created_at    REAL NOT NULL,
    due_at        REAL NOT NULL,
    started_at    REAL,
    completed_at  REAL,
    closed_at     REAL,
    voided_at     REAL,
    close_by      TEXT REFERENCES users(id),
    shift         TEXT NOT NULL DEFAULT '',
    resolution    TEXT NOT NULL DEFAULT '',
    void_reason   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_wo_door ON work_orders(door_id);
CREATE INDEX IF NOT EXISTS idx_wo_owner ON work_orders(owner_id);

-- 活动工单唯一占位：一门同时只能有一张未关闭/未废单的工单。
CREATE UNIQUE INDEX IF NOT EXISTS uq_door_active_wo
    ON work_orders(door_id)
    WHERE status IN ('open','in_progress','repair_done');

-- 工单备件行（计划量 / 预占量 / 已领量 / 已退量） ----------------------------

CREATE TABLE IF NOT EXISTS wo_parts (
    id            TEXT PRIMARY KEY,
    wo_id         TEXT NOT NULL REFERENCES work_orders(id),
    sku           TEXT NOT NULL,
    name          TEXT NOT NULL,
    planned_qty   INTEGER NOT NULL CHECK (planned_qty > 0),
    reserved_qty  INTEGER NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
    issued_qty    INTEGER NOT NULL DEFAULT 0 CHECK (issued_qty >= 0),
    returned_qty  INTEGER NOT NULL DEFAULT 0 CHECK (returned_qty >= 0),
    UNIQUE(wo_id, sku),
    CHECK (reserved_qty <= planned_qty),
    CHECK (issued_qty <= planned_qty),
    CHECK (returned_qty <= issued_qty)
);

CREATE INDEX IF NOT EXISTS idx_wop_sku ON wo_parts(sku);

-- 实物库存流水（只追加，重算库存的依据） --------------------------------------

CREATE TABLE IF NOT EXISTS inventory_ledger (
    id            TEXT PRIMARY KEY,
    sku           TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('receive','issue','return','adjust')),
    qty           INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    wo_id         TEXT,
    actor_id      TEXT NOT NULL,
    at            REAL NOT NULL,
    ref           TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ledger_sku ON inventory_ledger(sku, id);

-- 工单事件流（工单的完整真相，只追加） ----------------------------------------

CREATE TABLE IF NOT EXISTS wo_events (
    id        TEXT PRIMARY KEY,
    seq       INTEGER NOT NULL,
    wo_id     TEXT NOT NULL REFERENCES work_orders(id),
    kind      TEXT NOT NULL,
    actor_id  TEXT NOT NULL REFERENCES users(id),
    at        REAL NOT NULL,
    payload   TEXT NOT NULL DEFAULT '{}',
    UNIQUE(wo_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_wo ON wo_events(wo_id, seq);

-- 管理与权限审计（只追加，覆盖工单以外的全部敏感动作） ------------------------

CREATE TABLE IF NOT EXISTS audit_entries (
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    actor_id  TEXT NOT NULL,
    at        REAL NOT NULL,
    detail    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_entries(kind, at);

-- 超期提醒（只追加，天然去重依据） --------------------------------------------

CREATE TABLE IF NOT EXISTS reminders (
    id              TEXT PRIMARY KEY,
    wo_id           TEXT NOT NULL REFERENCES work_orders(id),
    level           INTEGER NOT NULL,
    target_user_id  TEXT NOT NULL REFERENCES users(id),
    kind            TEXT NOT NULL CHECK (kind IN ('due','done_unclosed')),
    created_at      REAL NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    -- 去重粒度包含目标人：改派后新负责人仍应收到本级提醒，同一人不重复收。
    UNIQUE(wo_id, level, kind, target_user_id)
);

-- 盘点单（库管发起、管理员批准的双人控制） ------------------------------------

CREATE TABLE IF NOT EXISTS stocktakes (
    id            TEXT PRIMARY KEY,
    sku           TEXT NOT NULL,
    expected_qty  INTEGER NOT NULL,
    counted_qty   INTEGER NOT NULL CHECK (counted_qty >= 0),
    status        TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')),
    created_by    TEXT NOT NULL REFERENCES users(id),
    created_at    REAL NOT NULL,
    decided_by    TEXT REFERENCES users(id),
    decided_at    REAL,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_stocktake_pending
    ON stocktakes(sku) WHERE status = 'pending';

-- 废单后已领未退的退料任务（必须人工确认） ------------------------------------

CREATE TABLE IF NOT EXISTS return_tasks (
    id            TEXT PRIMARY KEY,
    wo_id         TEXT NOT NULL REFERENCES work_orders(id),
    sku           TEXT NOT NULL,
    qty_out       INTEGER NOT NULL CHECK (qty_out >= 0),
    qty_returned  INTEGER NOT NULL DEFAULT 0 CHECK (qty_returned >= 0),
    status        TEXT NOT NULL CHECK (status IN ('pending','completed')),
    created_at    REAL NOT NULL,
    completed_at  REAL,
    CHECK (qty_returned <= qty_out)
);

CREATE INDEX IF NOT EXISTS idx_rtasks_status ON return_tasks(status);

-- 不可变触发器：只追加表禁改禁删 ----------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_wo_events_immutable_update
BEFORE UPDATE ON wo_events
BEGIN
    SELECT RAISE(ABORT, 'wo_events 为只追加历史，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_wo_events_immutable_delete
BEFORE DELETE ON wo_events
BEGIN
    SELECT RAISE(ABORT, 'wo_events 为只追加历史，禁止删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_immutable_update
BEFORE UPDATE ON inventory_ledger
BEGIN
    SELECT RAISE(ABORT, 'inventory_ledger 为只追加流水，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_immutable_delete
BEFORE DELETE ON inventory_ledger
BEGIN
    SELECT RAISE(ABORT, 'inventory_ledger 为只追加流水，禁止删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_immutable_update
BEFORE UPDATE ON audit_entries
BEGIN
    SELECT RAISE(ABORT, 'audit_entries 为只追加审计，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_immutable_delete
BEFORE DELETE ON audit_entries
BEGIN
    SELECT RAISE(ABORT, 'audit_entries 为只追加审计，禁止删除');
END;

CREATE TRIGGER IF NOT EXISTS trg_reminders_immutable_update
BEFORE UPDATE ON reminders
BEGIN
    SELECT RAISE(ABORT, 'reminders 为只追加记录，禁止修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_reminders_immutable_delete
BEFORE DELETE ON reminders
BEGIN
    SELECT RAISE(ABORT, 'reminders 为只追加记录，禁止删除');
END;
"""


def bootstrap(db) -> None:
    """在给定数据库上执行建表脚本（幂等）。"""

    db.executescript(SCHEMA_SQL)
