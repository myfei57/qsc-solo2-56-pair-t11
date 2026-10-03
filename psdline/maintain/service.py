"""工单与备件领域服务。

每个公开方法对应一个现场动作，方法内部自管事务（``BEGIN IMMEDIATE`` 起手）。
跨实体的一致性靠「同一事务 + 条件更新 + CHECK 约束」保证，不靠应用层内存锁：
进程重启、多个控制台同时操作，存储层结论都一样。

角色权限矩阵（详见各方法里的 :meth:`_require_role`）：

================  ========== ========== ============= ==========
动作              reporter   repair     storekeeper   manager
================  ========== ========== ============= ==========
报修开单            ✓          ✓          ✓             ✓
领件发料            -          -          ✓             ✓
完工                -          本人/接手    -             ✓
关单解锁            -          本人        -             ✓
改派（跨班交接）     -          -          -             ✓
废单                -          -          -             ✓
退料确认/入库        -          -          ✓             ✓
盘点发起            -          -          ✓             ✓
盘点批准            -          -          -             ✓
用户/门/备件建档     -          -          -             ✓
================  ========== ========== ============= ==========
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Sequence

from .db import Database
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .notify import Notifier, RecordingNotifier, Reminder

REPORTER = "reporter"
REPAIR = "repair"
STOREKEEPER = "storekeeper"
MANAGER = "manager"
ROLES = (REPORTER, REPAIR, STOREKEEPER, MANAGER)

# 仍占着门锁的活动状态（完工待关单也算活动：门还锁着）。
OPEN_STATES = ("open", "in_progress", "repair_done")
# 允许继续领/退料等「维修中」操作的状态：完工后账目冻结，等关单。
EDITABLE_STATES = ("open", "in_progress")

DEFAULT_SLA_HOURS = 24.0
# 第 1 级：负责人；超过升级窗口仍未闭环 → 第 2 级：管理员。
DEFAULT_ESCALATE_AFTER_HOURS = 24.0


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class MaintenanceService:
    """工单、备件、锁定、审计四位一体的领域服务。"""

    def __init__(
        self,
        db: Database,
        clock: Any | None = None,
        notifier: Notifier | None = None,
        *,
        sla_hours: float = DEFAULT_SLA_HOURS,
        escalate_after_hours: float = DEFAULT_ESCALATE_AFTER_HOURS,
        autoseed: bool = True,
    ) -> None:
        self.db = db
        self.clock = clock or _SystemClock()
        self.notifier: Notifier = notifier or RecordingNotifier()
        self.sla_hours = sla_hours
        self.escalate_after_hours = escalate_after_hours
        if autoseed:
            self.seed_if_empty()

    # ==================================================================
    # 基础档案
    # ==================================================================

    def create_user(
        self, actor_id: str, user_id: str, name: str, role: str
    ) -> dict:
        """建档一个用户（仅管理员）。"""

        self._require_role(actor_id, (MANAGER,))
        if not user_id or not name:
            raise ValidationError("用户标识与姓名不能为空")
        if role not in ROLES:
            raise ValidationError(f"未知角色: {role}")
        now = self._now()
        with self.db.transaction() as conn:
            self._exists(conn, "users", actor_id, "用户")
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
                raise ConflictError(f"用户已存在: {user_id}")
            conn.execute(
                "INSERT INTO users(id, name, role, active, created_at, created_by)"
                " VALUES (?, ?, ?, 1, ?, ?)",
                (user_id, name, role, now, actor_id),
            )
            self._audit(conn, "user_create", actor_id, user_id=user_id, role=role)
        return self.get_user(user_id)

    def create_door(
        self, actor_id: str, door_id: str, name: str, location: str = ""
    ) -> dict:
        """登记一樘门（仅管理员）。"""

        self._require_role(actor_id, (MANAGER,))
        if not door_id or not name:
            raise ValidationError("门标识与名称不能为空")
        now = self._now()
        with self.db.transaction() as conn:
            self._exists(conn, "users", actor_id, "用户")
            if conn.execute("SELECT 1 FROM doors WHERE id = ?", (door_id,)).fetchone():
                raise ConflictError(f"门已登记: {door_id}")
            conn.execute(
                "INSERT INTO doors(id, name, location, locked, active_wo, created_at)"
                " VALUES (?, ?, ?, 0, NULL, ?)",
                (door_id, name, location, now),
            )
            self._audit(conn, "door_create", actor_id, door_id=door_id)
        return self.get_door(door_id)

    def create_part(
        self,
        actor_id: str,
        sku: str,
        name: str,
        initial_qty: int = 0,
        unit: str = "件",
    ) -> dict:
        """登记一个备件品种，可同时做首次入库（仅管理员建档）。"""

        self._require_role(actor_id, (MANAGER,))
        self._positive(initial_qty, "初始库存", allow_zero=True)
        if not sku or not name:
            raise ValidationError("备件编码与名称不能为空")
        now = self._now()
        with self.db.transaction() as conn:
            self._exists(conn, "users", actor_id, "用户")
            if conn.execute("SELECT 1 FROM parts WHERE sku = ?", (sku,)).fetchone():
                raise ConflictError(f"备件已存在: {sku}")
            part_id = _new_id("part")
            conn.execute(
                "INSERT INTO parts(id, sku, name, unit, on_hand, reserved_qty, created_at)"
                " VALUES (?, ?, ?, ?, ?, 0, ?)",
                (part_id, sku, name, unit, initial_qty, now),
            )
            if initial_qty > 0:
                self._ledger(conn, sku, "receive", initial_qty, initial_qty, actor_id, now,
                             ref="建档首次入库")
            self._audit(conn, "part_create", actor_id, sku=sku, initial_qty=initial_qty)
        return self.get_part(sku)

    def receive_stock(
        self, actor_id: str, sku: str, qty: int, note: str = ""
    ) -> dict:
        """备件入库补货（库管或管理员），写实物流水。"""

        self._require_role(actor_id, (STOREKEEPER, MANAGER))
        self._positive(qty, "入库数量")
        now = self._now()
        with self.db.transaction() as conn:
            row = self._lock_part(conn, sku)
            new_balance = row["on_hand"] + qty
            conn.execute(
                "UPDATE parts SET on_hand = ? WHERE sku = ?", (new_balance, sku)
            )
            self._ledger(conn, sku, "receive", qty, new_balance, actor_id, now, note=note)
            self._audit(conn, "stock_receive", actor_id, sku=sku, qty=qty)
        return self.get_part(sku)

    # ==================================================================
    # 工单生命周期
    # ==================================================================

    def open_work_order(
        self,
        actor_id: str,
        door_id: str,
        title: str,
        parts: Sequence[tuple[str, int]] | dict[str, int] | None = None,
        *,
        owner_id: str | None = None,
        description: str = "",
        sla_hours: float | None = None,
        shift: str = "",
    ) -> dict:
        """门区报修开单：校验可用量 → 预占备件 → 工单落账 → 门上锁。

        任一备件可用量不足，整笔回滚（门不会锁、预占不会发生、工单不存在）。
        """

        self._require_role(actor_id, (REPORTER, REPAIR, STOREKEEPER, MANAGER))
        if not title:
            raise ValidationError("工单标题不能为空")
        lines = self._normalize_lines(parts)
        sla = self.sla_hours if sla_hours is None else sla_hours
        self._positive_number(sla, "SLA 小时数")
        now = self._now()
        due_at = now + sla * 3600.0

        with self.db.transaction() as conn:
            self._exists(conn, "users", actor_id, "报修人")
            door = self._lock_door(conn, door_id)
            if door["locked"] == 1:
                raise ConflictError(
                    f"门 {door_id} 已被活动工单 {door['active_wo']} 锁定，禁止重复开单"
                )
            owner = owner_id or actor_id
            owner_row = conn.execute(
                "SELECT 1 FROM users WHERE id = ? AND active = 1", (owner,)
            ).fetchone()
            if owner_row is None:
                raise ValidationError(f"负责人不存在或已停用: {owner}")

            # 按 SKU 排序后逐一加预占，保证与领料等其它事务的加锁顺序一致，杜绝死锁。
            for sku, qty in sorted(lines.items()):
                row = self._lock_part(conn, sku)
                available = row["on_hand"] - row["reserved_qty"]
                if available < qty:
                    raise ConflictError(
                        f"备件 {sku} 可用量不足：需要 {qty}，可用 {available}"
                        f"（实物 {row['on_hand']}，已预占 {row['reserved_qty']}）"
                    )

            wo_id = _new_id("wo")
            code = self._allocate_code(conn, now)
            conn.execute(
                "INSERT INTO work_orders(id, code, door_id, title, description, status,"
                " reported_by, owner_id, created_at, due_at, shift)"
                " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                (wo_id, code, door_id, title, description, actor_id, owner, now, due_at, shift),
            )
            for sku, qty in sorted(lines.items()):
                row = self._lock_part(conn, sku)
                conn.execute(
                    "UPDATE parts SET reserved_qty = reserved_qty + ? WHERE sku = ?",
                    (qty, sku),
                )
                conn.execute(
                    "INSERT INTO wo_parts(id, wo_id, sku, name, planned_qty, reserved_qty)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (_new_id("wop"), wo_id, sku, row["name"], qty, qty),
                )
            conn.execute(
                "UPDATE doors SET locked = 1, active_wo = ? WHERE id = ?", (wo_id, door_id)
            )
            self._event(conn, wo_id, "opened", actor_id, now,
                        owner_id=owner, shift=shift, parts=lines, due_at=due_at)
            self._audit(conn, "wo_open", actor_id, wo_id=wo_id, door_id=door_id,
                        owner_id=owner, parts=lines)
        return self.get_work_order(wo_id)

    def issue_parts(
        self,
        actor_id: str,
        wo_id: str,
        items: Sequence[tuple[str, int]] | dict[str, int],
        *,
        note: str = "",
    ) -> dict:
        """领料发料（库管）：扣实物 + 减预占 + 写流水，整笔原子。

        多件领料里只要一件超领或实物不足，全部回滚——不会出现「A 件扣了、B 件
        不够」的半成品状态。
        """

        self._require_role(actor_id, (STOREKEEPER, MANAGER))
        wanted = self._normalize_lines(items)
        now = self._now()
        with self.db.transaction() as conn:
            wo = self._lock_wo(conn, wo_id)
            self._require_editable(wo)
            for sku, qty in sorted(wanted.items()):
                line = conn.execute(
                    "SELECT * FROM wo_parts WHERE wo_id = ? AND sku = ?", (wo_id, sku)
                ).fetchone()
                if line is None:
                    raise ValidationError(f"工单 {wo_id} 没有备件行 {sku}，需先改单追加")
                issuable = line["planned_qty"] - line["issued_qty"]
                if qty > issuable:
                    raise ConflictError(
                        f"备件 {sku} 超领：本次 {qty}，该工单还可领 {issuable}"
                    )
            issued: dict[str, int] = {}
            for sku, qty in sorted(wanted.items()):
                part = self._lock_part(conn, sku)
                if part["on_hand"] < qty:
                    # 正常流程下预占保证不会走到这里；走到说明实物账与预占脱节，
                    # 整笔回滚并交核对处理，绝不允许扣成负数。
                    raise ConflictError(
                        f"备件 {sku} 实物不足：要发 {qty}，实物 {part['on_hand']}，请核对库存"
                    )
                if part["reserved_qty"] < qty:
                    # 预占已被释放（例如工单已完工/废单）时会走到这里：明确报错，
                    # 不靠 CHECK 约束抛底层 IntegrityError。
                    raise ConflictError(
                        f"备件 {sku} 已无有效预占（预占 {part['reserved_qty']}），"
                        "该工单可能已完工或废单"
                    )
                new_balance = part["on_hand"] - qty
                conn.execute(
                    "UPDATE parts SET on_hand = ?, reserved_qty = reserved_qty - ? WHERE sku = ?",
                    (new_balance, qty, sku),
                )
                conn.execute(
                    "UPDATE wo_parts SET issued_qty = issued_qty + ?,"
                    " reserved_qty = reserved_qty - ? WHERE wo_id = ? AND sku = ?",
                    (qty, qty, wo_id, sku),
                )
                self._ledger(conn, sku, "issue", -qty, new_balance, actor_id, now,
                             wo_id=wo_id, note=note)
                issued[sku] = qty
            self._event(conn, wo_id, "parts_issued", actor_id, now, items=issued, note=note)
            self._audit(conn, "parts_issue", actor_id, wo_id=wo_id, items=issued)
        return self.get_work_order(wo_id)

    def start_work(self, actor_id: str, wo_id: str, *, shift: str | None = None) -> dict:
        """签收工单、开始维修（负责人或管理员）。"""

        now = self._now()
        with self.db.transaction() as conn:
            wo = self._lock_wo(conn, wo_id)
            self._require_owner_or_manager(actor_id, wo)
            if wo["status"] != "open":
                raise ConflictError(f"只有 open 工单可以开始维修，当前 {wo['status']}")
            conn.execute("UPDATE work_orders SET status = 'in_progress', started_at = ? WHERE id = ?",
                         (now, wo_id))
            payload: dict[str, Any] = {}
            if shift is not None:
                payload["shift"] = shift
                conn.execute("UPDATE work_orders SET shift = ? WHERE id = ?", (shift, wo_id))
            self._event(conn, wo_id, "started", actor_id, now, **payload)
            self._audit(conn, "wo_start", actor_id, wo_id=wo_id)
        return self.get_work_order(wo_id)

    def complete_work(self, actor_id: str, wo_id: str, resolution: str = "") -> dict:
        """维修完工（负责人或管理员）。

        门**不解锁**：完工只是维修动作结束，门要等关单验收后才放行。完工时剩余
        未领用的预占自动释放（实物仍在库，预占只是还给公共可用池）。
        """

        if not resolution:
            raise ValidationError("完工说明不能为空")
        now = self._now()
        with self.db.transaction() as conn:
            wo = self._lock_wo(conn, wo_id)
            self._require_owner_or_manager(actor_id, wo)
            if wo["status"] not in ("in_progress", "open"):
                raise ConflictError(f"当前状态 {wo['status']} 不允许完工")
            self._release_remaining(conn, wo_id)
            conn.execute(
                "UPDATE work_orders SET status = 'repair_done', completed_at = ?,"
                " resolution = ? WHERE id = ?",
                (now, resolution, wo_id),
            )
            self._event(conn, wo_id, "completed", actor_id, now, resolution=resolution)
            self._audit(conn, "wo_complete", actor_id, wo_id=wo_id)
        return self.get_work_order(wo_id)

    def close_work_order(self, actor_id: str, wo_id: str) -> dict:
        """验收关单：只有这一步（或废单）解除门的维修锁。

        允许两种路径：直接关 ``open``/``in_progress``（免修、带件关闭等，由关单人
        负责），或关 ``repair_done``。关单时残留预占一律释放。权限：负责人本人或
        管理员——报修人不能自己关自己开的单。
        """

        now = self._now()
        with self.db.transaction() as conn:
            wo = self._lock_wo(conn, wo_id)
            self._require_owner_or_manager(actor_id, wo)
            if wo["status"] not in OPEN_STATES:
                raise ConflictError(f"只有未终结工单可以关单，当前 {wo['status']}")
            self._release_remaining(conn, wo_id)
            conn.execute(
                "UPDATE work_orders SET status = 'closed', closed_at = ?, close_by = ?"
                " WHERE id = ?",
                (now, actor_id, wo_id),
            )
            conn.execute(
                "UPDATE doors SET locked = 0, active_wo = NULL WHERE id = ?", (wo["door_id"],)
            )
            self._event(conn, wo_id, "closed", actor_id, now)
            self._audit(conn, "wo_close", actor_id, wo_id=wo_id, door_id=wo["door_id"])
        return self.get_work_order(wo_id)

    def reassign(
        self, actor_id: str, wo_id: str, new_owner_id: str, *, shift: str = "", note: str = ""
    ) -> dict:
        """跨班改派负责人（仅管理员）。原负责人与新负责人的交接留在事件流里。"""

        now = self._now()
        with self.db.transaction() as conn:
            self._require_role(actor_id, (MANAGER,))
            wo = self._lock_wo(conn, wo_id)
            if wo["status"] not in OPEN_STATES:
                raise ConflictError(f"工单已终结（{wo['status']}），不能改派")
            if conn.execute(
                "SELECT 1 FROM users WHERE id = ? AND active = 1", (new_owner_id,)
            ).fetchone() is None:
                raise ValidationError(f"新负责人不存在或已停用: {new_owner_id}")
            old_owner = wo["owner_id"]
            conn.execute(
                "UPDATE work_orders SET owner_id = ?, shift = ? WHERE id = ?",
                (new_owner_id, shift or wo["shift"], wo_id),
            )
            self._event(conn, wo_id, "reassigned", actor_id, now,
                        **{"from": old_owner, "to": new_owner_id, "shift": shift, "note": note})
            self._audit(conn, "wo_reassign", actor_id, wo_id=wo_id,
                        **{"from": old_owner, "to": new_owner_id})
        return self.get_work_order(wo_id)

    def void_work_order(
        self, actor_id: str, wo_id: str, reason: str
    ) -> dict:
        """废单（仅管理员）。

        - 未领用的预占：自动释放（件本来就在库里，只是冻结，无账实风险）；
        - 已领用的件：**不自动回补**，按「已领未退」数量生成退料任务，库管必须
          逐笔确认退回后库存才增加。件可能已被用掉或损坏，自动回补等于造假账。
        废单同时解除门的维修锁。
        """

        if not reason:
            raise ValidationError("废单原因不能为空")
        now = self._now()
        with self.db.transaction() as conn:
            wo = self._lock_wo(conn, wo_id)
            self._require_role(actor_id, (MANAGER,))
            if wo["status"] not in OPEN_STATES:
                raise ConflictError(f"工单已终结（{wo['status']}），不能废单")
            self._release_remaining(conn, wo_id)
            pending_returns: dict[str, int] = {}
            lines = conn.execute(
                "SELECT sku, issued_qty, returned_qty FROM wo_parts WHERE wo_id = ?", (wo_id,)
            ).fetchall()
            for line in lines:
                outstanding = line["issued_qty"] - line["returned_qty"]
                if outstanding > 0:
                    conn.execute(
                        "INSERT INTO return_tasks(id, wo_id, sku, qty_out, qty_returned,"
                        " status, created_at)"
                        " VALUES (?, ?, ?, ?, 0, 'pending', ?)",
                        (_new_id("rt"), wo_id, line["sku"], outstanding, now),
                    )
                    pending_returns[line["sku"]] = outstanding
            conn.execute(
                "UPDATE work_orders SET status = 'void', voided_at = ?, void_reason = ?"
                " WHERE id = ?",
                (now, reason, wo_id),
            )
            conn.execute(
                "UPDATE doors SET locked = 0, active_wo = NULL WHERE id = ?", (wo["door_id"],)
            )
            self._event(conn, wo_id, "voided", actor_id, now,
                        reason=reason, pending_returns=pending_returns)
            self._audit(conn, "wo_void", actor_id, wo_id=wo_id,
                        door_id=wo["door_id"], pending_returns=pending_returns)
        return self.get_work_order(wo_id)

    # ==================================================================
    # 退料（废单后人工确认）
    # ==================================================================

    def confirm_return(
        self, actor_id: str, task_id: str, qty: int | None = None, *, note: str = ""
    ) -> dict:
        """库管确认退料：实物点收入库，库存回补、写流水。

        可分多次退（退回 3 件、再退回 2 件）；全部退清时任务完成。件确实找不回的
        部分留在任务上，必须走盘点/调整流程处理，系统不会默默吞掉。
        """

        self._require_role(actor_id, (STOREKEEPER, MANAGER))
        now = self._now()
        with self.db.transaction() as conn:
            task = conn.execute("SELECT * FROM return_tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError(f"退料任务不存在: {task_id}")
            if task["status"] != "pending":
                raise ConflictError("退料任务已完成")
            remaining = task["qty_out"] - task["qty_returned"]
            qty = remaining if qty is None else qty
            self._positive(qty, "退料数量")
            if qty > remaining:
                raise ConflictError(f"退料数量 {qty} 超过待退 {remaining}")
            part = self._lock_part(conn, task["sku"])
            new_balance = part["on_hand"] + qty
            conn.execute("UPDATE parts SET on_hand = ? WHERE sku = ?", (new_balance, task["sku"]))
            new_returned = task["qty_returned"] + qty
            finished = new_returned >= task["qty_out"]
            conn.execute(
                "UPDATE return_tasks SET qty_returned = ?, status = ?, completed_at = ?"
                " WHERE id = ?",
                (new_returned, "completed" if finished else "pending", now if finished else None, task_id),
            )
            conn.execute(
                "UPDATE wo_parts SET returned_qty = returned_qty + ? WHERE wo_id = ? AND sku = ?",
                (qty, task["wo_id"], task["sku"]),
            )
            self._ledger(conn, task["sku"], "return", qty, new_balance, actor_id, now,
                         wo_id=task["wo_id"], ref=f"退料任务 {task_id}", note=note)
            self._event(conn, task["wo_id"], "parts_returned", actor_id, now,
                        sku=task["sku"], qty=qty, finished=finished)
            self._audit(conn, "return_confirm", actor_id, task_id=task_id,
                        wo_id=task["wo_id"], sku=task["sku"], qty=qty, finished=finished)
        return self.get_return_task(task_id)

    # ==================================================================
    # 盘点（双人控制：库管发起，管理员批准）
    # ==================================================================

    def create_stocktake(
        self, actor_id: str, sku: str, counted_qty: int, *, note: str = ""
    ) -> dict:
        """库管发起盘点，登记实物盘点数（不动账）。同一 SKU 同时只允许一个待批单。"""

        self._require_role(actor_id, (STOREKEEPER, MANAGER))
        self._positive(counted_qty, "盘点数量", allow_zero=True)
        now = self._now()
        with self.db.transaction() as conn:
            part = self._lock_part(conn, sku)
            pending = conn.execute(
                "SELECT 1 FROM stocktakes WHERE sku = ? AND status = 'pending'", (sku,)
            ).fetchone()
            if pending is not None:
                raise ConflictError(f"备件 {sku} 已有待批准盘点单")
            take_id = _new_id("take")
            conn.execute(
                "INSERT INTO stocktakes(id, sku, expected_qty, counted_qty, status,"
                " created_by, created_at, note)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (take_id, sku, part["on_hand"], counted_qty, actor_id, now, note),
            )
            self._audit(conn, "stocktake_create", actor_id, sku=sku,
                        expected=part["on_hand"], counted=counted_qty)
        return self.get_stocktake(take_id)

    def decide_stocktake(
        self, actor_id: str, take_id: str, approve: bool, *, note: str = ""
    ) -> dict:
        """管理员批准/驳回盘点。批准时把账面调整到实物数，写调整流水（带差额）。"""

        self._require_role(actor_id, (MANAGER,))
        now = self._now()
        with self.db.transaction() as conn:
            take = conn.execute(
                "SELECT * FROM stocktakes WHERE id = ?", (take_id,)
            ).fetchone()
            if take is None:
                raise NotFoundError(f"盘点单不存在: {take_id}")
            if take["status"] != "pending":
                raise ConflictError("盘点单已处理")
            status = "approved" if approve else "rejected"
            final_note = note or take["note"]
            conn.execute(
                "UPDATE stocktakes SET status = ?, decided_by = ?, decided_at = ?,"
                " note = ? WHERE id = ?",
                (status, actor_id, now, final_note, take_id),
            )
            delta = 0
            if approve:
                part = self._lock_part(conn, take["sku"])
                delta = take["counted_qty"] - part["on_hand"]
                conn.execute(
                    "UPDATE parts SET on_hand = ? WHERE sku = ?",
                    (take["counted_qty"], take["sku"]),
                )
                if delta != 0:
                    self._ledger(conn, take["sku"], "adjust", delta, take["counted_qty"],
                                 actor_id, now, ref=f"盘点 {take_id}", note=note)
            self._audit(conn, "stocktake_decide", actor_id, take_id=take_id,
                        sku=take["sku"], approved=approve, delta=delta)
        return self.get_stocktake(take_id)

    # ==================================================================
    # 超期提醒
    # ==================================================================

    def scan_overdue(self, *, now: float | None = None) -> list[dict]:
        """扫描全部活动工单，按 SLA 与升级窗口投递去重提醒。

        两级提醒，每级每单每种情形对每个负责人只发一次（``reminders`` 表
        ``UNIQUE(wo_id, level, kind, target_user_id)``）：

        - level 1 ``due``：超过 ``due_at`` 仍未完工 → 负责人；
        - level 1 ``done_unclosed``：完工后超过升级窗口仍未关单（门还锁着）→ 负责人；
        - level 2：上述情形再经过一个升级窗口仍未闭环 → 管理员。
          若一张单的 due 已升级、后来完工又超期未关，会再收到
          ``done_unclosed`` 的逐级提醒，因为门始终锁着，两种超期都要被看见。
        """

        moment = self._now() if now is None else now
        due: list[Reminder] = []
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM work_orders WHERE status IN ('open','in_progress','repair_done')"
            ).fetchall()
            for wo in rows:
                kind: str | None = None
                overdue_since: float | None = None
                if wo["status"] == "repair_done":
                    if moment >= wo["completed_at"] + self.escalate_after_hours * 3600.0:
                        kind = "done_unclosed"
                        overdue_since = wo["completed_at"] + self.escalate_after_hours * 3600.0
                elif moment >= wo["due_at"]:
                    kind = "due"
                    overdue_since = wo["due_at"]
                if kind is None:
                    continue
                levels = [1]
                if moment >= overdue_since + self.escalate_after_hours * 3600.0:
                    levels.append(2)
                for level in levels:
                    target = wo["owner_id"] if level == 1 else self._manager_id(conn)
                    if target is None:
                        continue
                    if conn.execute(
                        "SELECT 1 FROM reminders WHERE wo_id = ? AND level = ? AND kind = ?"
                        " AND target_user_id = ?",
                        (wo["id"], level, kind, target),
                    ).fetchone() is not None:
                        continue
                    reminder = Reminder(
                        wo_id=wo["id"],
                        level=level,
                        target_user_id=target,
                        kind=kind,
                        at=moment,
                        note=self._reminder_note(wo, kind, level),
                    )
                    conn.execute(
                        "INSERT INTO reminders(id, wo_id, level, target_user_id, kind,"
                        " created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (_new_id("rm"), reminder.wo_id, level, reminder.target_user_id,
                         reminder.kind, moment, reminder.note),
                    )
                    due.append(reminder)
        # 投递放在事务外：外部通知失败不应回滚业务事实。
        for reminder in due:
            self.notifier.send(reminder)
        return [item.as_dict() for item in due]

    # ==================================================================
    # 一致性核对与恢复
    # ==================================================================

    def verify_consistency(self) -> dict:
        """核对「只追加真相」与「状态投影」是否一致，返回全部偏差。

        - 门：锁定位必须与活动工单互为镜像；
        - 备件：投影实物必须等于流水重算结存；预占必须等于活动工单未领预占之和；
        - 工单：备件行「预占 + 已领」不得超过计划量；状态与门锁必须同向。
        发现偏差只报告、不自动改账——库存对不上时先交盘点双人确认，避免「恢复」
        动作本身毁掉证据。
        """

        violations: list[dict] = []
        with self.db.transaction() as conn:
            for door in conn.execute("SELECT * FROM doors").fetchall():
                active = conn.execute(
                    "SELECT id FROM work_orders WHERE door_id = ?"
                    " AND status IN ('open','in_progress','repair_done')",
                    (door["id"],),
                ).fetchone()
                active_id = active["id"] if active else None
                if door["locked"] == 1 and active_id is None:
                    violations.append({"kind": "door_locked_without_wo", "door_id": door["id"]})
                if door["locked"] == 0 and active_id is not None:
                    violations.append({"kind": "door_unlocked_with_active_wo",
                                       "door_id": door["id"], "wo_id": active_id})
                if door["active_wo"] != active_id:
                    violations.append({"kind": "door_active_wo_drift", "door_id": door["id"],
                                       "stored": door["active_wo"], "expected": active_id})

            for part in conn.execute("SELECT * FROM parts").fetchall():
                ledger_balance = conn.execute(
                    "SELECT COALESCE(SUM(qty), 0) FROM inventory_ledger WHERE sku = ?",
                    (part["sku"],),
                ).fetchone()[0]
                if ledger_balance != part["on_hand"]:
                    violations.append({"kind": "stock_ledger_drift", "sku": part["sku"],
                                       "projection": part["on_hand"],
                                       "ledger": ledger_balance})
                reserved_open = conn.execute(
                    "SELECT COALESCE(SUM(wp.reserved_qty), 0) FROM wo_parts wp"
                    " JOIN work_orders wo ON wo.id = wp.wo_id"
                    " WHERE wp.sku = ? AND wo.status IN ('open','in_progress','repair_done')",
                    (part["sku"],),
                ).fetchone()[0]
                if reserved_open != part["reserved_qty"]:
                    violations.append({"kind": "reservation_drift", "sku": part["sku"],
                                       "projection": part["reserved_qty"],
                                       "expected": reserved_open})
                if part["reserved_qty"] > part["on_hand"]:
                    violations.append({"kind": "reserved_exceeds_stock", "sku": part["sku"]})

            for line in conn.execute("SELECT * FROM wo_parts").fetchall():
                if line["issued_qty"] > line["planned_qty"]:
                    violations.append({"kind": "over_issued", "wo_id": line["wo_id"],
                                       "sku": line["sku"]})
                if line["reserved_qty"] + line["issued_qty"] > line["planned_qty"]:
                    violations.append({"kind": "reservation_line_drift", "wo_id": line["wo_id"],
                                       "sku": line["sku"]})

            for wo in conn.execute("SELECT * FROM work_orders").fetchall():
                door = conn.execute("SELECT * FROM doors WHERE id = ?", (wo["door_id"],)).fetchone()
                if wo["status"] in OPEN_STATES and door["locked"] == 0:
                    violations.append({"kind": "active_wo_without_lock", "wo_id": wo["id"]})
                if wo["status"] in ("closed", "void") and door["active_wo"] == wo["id"]:
                    violations.append({"kind": "finished_wo_still_locks", "wo_id": wo["id"]})

        return {"ok": not violations, "violations": violations,
                "checked_at": self._now()}

    def rebuild_projections(self, actor_id: str) -> dict:
        """从只追加流水/事件重建可推导的投影（恢复手段，需管理员）。

        重建范围仅限**可从历史推导**的量：备件实物（流水求和）、预占（活动工单
        备件行求和）。门的锁定位以「活动工单是否存在」为准同步。盘点调整已经在
        流水里，所以重建后账与批准过的盘点一致；未经批准的实物差异不会被悄悄
        抹平，需要走盘点流程。
        """

        self._require_role(actor_id, (MANAGER,))
        fixed: list[str] = []
        with self.db.transaction() as conn:
            self._exists(conn, "users", actor_id, "用户")
            for part in conn.execute("SELECT sku FROM parts").fetchall():
                sku = part["sku"]
                on_hand = conn.execute(
                    "SELECT COALESCE(SUM(qty), 0) FROM inventory_ledger WHERE sku = ?", (sku,)
                ).fetchone()[0]
                reserved = conn.execute(
                    "SELECT COALESCE(SUM(wp.reserved_qty), 0) FROM wo_parts wp"
                    " JOIN work_orders wo ON wo.id = wp.wo_id WHERE wp.sku = ?"
                    " AND wo.status IN ('open','in_progress','repair_done')",
                    (sku,),
                ).fetchone()[0]
                current = conn.execute("SELECT on_hand, reserved_qty FROM parts WHERE sku = ?",
                                       (sku,)).fetchone()
                if current["on_hand"] != on_hand or current["reserved_qty"] != reserved:
                    conn.execute("UPDATE parts SET on_hand = ?, reserved_qty = ? WHERE sku = ?",
                                 (on_hand, reserved, sku))
                    fixed.append(f"{sku}: 实物 {current['on_hand']}->{on_hand},"
                                 f" 预占 {current['reserved_qty']}->{reserved}")
            for door in conn.execute("SELECT id FROM doors").fetchall():
                active = conn.execute(
                    "SELECT id FROM work_orders WHERE door_id = ?"
                    " AND status IN ('open','in_progress','repair_done')",
                    (door["id"],),
                ).fetchone()
                locked = 1 if active else 0
                active_wo = active["id"] if active else None
                conn.execute("UPDATE doors SET locked = ?, active_wo = ? WHERE id = ?",
                             (locked, active_wo, door["id"]))
            self._audit(conn, "rebuild_projections", actor_id, fixed=fixed)
        report = self.verify_consistency()
        return {"fixed": fixed, "verify": report}

    # ==================================================================
    # 查询
    # ==================================================================

    def get_user(self, user_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
        if row is None:
            raise NotFoundError(f"用户不存在: {user_id}")
        return dict(row)

    def get_door(self, door_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM doors WHERE id = ?", (door_id,))
        if row is None:
            raise NotFoundError(f"门不存在: {door_id}")
        return self._door_dict(row)

    def get_part(self, sku: str) -> dict:
        row = self.db.query_one("SELECT * FROM parts WHERE sku = ?", (sku,))
        if row is None:
            raise NotFoundError(f"备件不存在: {sku}")
        return self._part_dict(row)

    def list_parts(self) -> list[dict]:
        return [self._part_dict(row) for row in
                self.db.query_all("SELECT * FROM parts ORDER BY sku")]

    def list_doors(self) -> list[dict]:
        return [self._door_dict(row) for row in
                self.db.query_all("SELECT * FROM doors ORDER BY id")]

    def list_work_orders(self, status: str | None = None) -> list[dict]:
        if status is not None:
            rows = self.db.query_all(
                "SELECT * FROM work_orders WHERE status = ? ORDER BY created_at, rowid", (status,))
        else:
            rows = self.db.query_all("SELECT * FROM work_orders ORDER BY created_at, rowid")
        return [self._wo_dict(row) for row in rows]

    def get_work_order(self, wo_id: str, *, with_history: bool = True) -> dict:
        row = self.db.query_one("SELECT * FROM work_orders WHERE id = ?", (wo_id,))
        if row is None:
            raise NotFoundError(f"工单不存在: {wo_id}")
        result = self._wo_dict(row)
        result["parts"] = [
            dict(line) for line in self.db.query_all(
                "SELECT sku, name, planned_qty, reserved_qty, issued_qty, returned_qty"
                " FROM wo_parts WHERE wo_id = ? ORDER BY sku", (wo_id,))
        ]
        if with_history:
            result["history"] = self.work_order_history(wo_id)
        return result

    def work_order_history(self, wo_id: str) -> list[dict]:
        """工单事件流（不可变，只追加，按序号升序）。"""

        rows = self.db.query_all(
            "SELECT e.id, e.seq, e.kind, e.actor_id, e.at, e.payload, u.name AS actor_name"
            " FROM wo_events e LEFT JOIN users u ON u.id = e.actor_id"
            " WHERE e.wo_id = ? ORDER BY e.seq", (wo_id,))
        result = []
        for row in rows:
            item = {key: row[key] for key in ("id", "seq", "kind", "actor_id", "actor_name", "at")}
            item["payload"] = json.loads(row["payload"])
            result.append(item)
        return result

    def ledger(self, sku: str | None = None, limit: int = 200) -> list[dict]:
        """实物库存流水（只追加）。

        排序用 ``rowid DESC``（插入序）而不是 ``at DESC, id DESC``：同一时刻写入
        的多条流水时间戳相同，而 id 是随机串，按随机串并列排序会让「最新一条」
        不稳定。rowid 是 SQLite 给追加行的单调递增序，正好等于写入先后。
        """

        if sku is None:
            rows = self.db.query_all(
                "SELECT * FROM inventory_ledger ORDER BY rowid DESC LIMIT ?", (limit,))
        else:
            rows = self.db.query_all(
                "SELECT * FROM inventory_ledger WHERE sku = ? ORDER BY rowid DESC LIMIT ?",
                (sku, limit))
        return [dict(row) for row in rows]

    def audit_trail(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        """管理审计（只追加），按写入先后倒序。"""

        if kind is None:
            rows = self.db.query_all(
                "SELECT * FROM audit_entries ORDER BY rowid DESC LIMIT ?", (limit,))
        else:
            rows = self.db.query_all(
                "SELECT * FROM audit_entries WHERE kind = ? ORDER BY rowid DESC LIMIT ?",
                (kind, limit))
        return [self._audit_dict(row) for row in rows]

    def list_return_tasks(self, pending_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM return_tasks"
        if pending_only:
            sql += " WHERE status = 'pending'"
        sql += " ORDER BY created_at, rowid"
        return [dict(row) for row in self.db.query_all(sql)]

    def get_return_task(self, task_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM return_tasks WHERE id = ?", (task_id,))
        if row is None:
            raise NotFoundError(f"退料任务不存在: {task_id}")
        return dict(row)

    def list_stocktakes(self, status: str | None = None) -> list[dict]:
        if status is None:
            rows = self.db.query_all("SELECT * FROM stocktakes ORDER BY created_at DESC, rowid DESC")
        else:
            rows = self.db.query_all(
                "SELECT * FROM stocktakes WHERE status = ? ORDER BY created_at DESC, rowid DESC", (status,))
        return [dict(row) for row in rows]

    def get_stocktake(self, take_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM stocktakes WHERE id = ?", (take_id,))
        if row is None:
            raise NotFoundError(f"盘点单不存在: {take_id}")
        return dict(row)

    def list_reminders(self, wo_id: str | None = None) -> list[dict]:
        if wo_id is None:
            rows = self.db.query_all("SELECT * FROM reminders ORDER BY created_at, rowid")
        else:
            rows = self.db.query_all(
                "SELECT * FROM reminders WHERE wo_id = ? ORDER BY created_at, rowid", (wo_id,))
        return [dict(row) for row in rows]

    # ==================================================================
    # 装配：演示种子数据
    # ==================================================================

    def seed_if_empty(self) -> None:
        """空库时种入四个角色账号、几樘门和常用备件（演示/交付检查用）。"""

        now = self._now()
        with self.db.transaction() as conn:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
                return
            users = [
                ("u-report", "王报修", REPORTER),
                ("u-repair", "李维修", REPAIR),
                ("u-store", "赵库管", STOREKEEPER),
                ("u-admin", "陈管理", MANAGER),
            ]
            for uid, name, role in users:
                conn.execute(
                    "INSERT INTO users(id, name, role, active, created_at, created_by)"
                    " VALUES (?, ?, ?, 1, ?, NULL)", (uid, name, role, now))
            for did, name, location in (
                ("D-PSD-0101", "1 站台 1 号门", "ST1/G01"),
                ("D-PSD-0102", "1 站台 2 号门", "ST1/G01"),
                ("D-PSD-0201", "2 站台 1 号门", "ST2/G01"),
            ):
                conn.execute(
                    "INSERT INTO doors(id, name, location, locked, active_wo, created_at)"
                    " VALUES (?, ?, ?, 0, NULL, ?)", (did, name, location, now))
            parts = (
                ("MTR-DC110", "110V 直流电机", 5),
                ("BELT-1200", "传动皮带 1200mm", 12),
                ("CTRL-PCB-A", "门控板 A 型", 4),
                ("SEN-IR-24", "红外对射传感器", 20),
                ("BOLT-M8x30", "M8x30 螺栓", 200),
            )
            for sku, name, qty in parts:
                conn.execute(
                    "INSERT INTO parts(id, sku, name, unit, on_hand, reserved_qty, created_at)"
                    " VALUES (?, ?, ?, '件', ?, 0, ?)",
                    (_new_id("part"), sku, name, qty, now),
                )
                self._ledger(conn, sku, "receive", qty, qty, "u-admin", now, ref="期初入库")
            self._audit(conn, "seed", "u-admin", users=[u[0] for u in users],
                        doors=3, parts=len(parts))

    # ==================================================================
    # 内部辅助
    # ==================================================================

    def _now(self) -> float:
        return float(self.clock.now())

    @staticmethod
    def _positive(value: float, label: str, *, allow_zero: bool = False) -> None:
        """校验整数数量（库存、件数）。"""

        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"{label}必须是整数")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError(f"{label}必须{'非负' if allow_zero else '为正'}整数")

    @staticmethod
    def _positive_number(value: float, label: str) -> None:
        """校验正数（SLA、小时数这类允许小数的量）。"""

        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{label}必须为正数")

    @staticmethod
    def _normalize_lines(
        items: Sequence[tuple[str, int]] | dict[str, int] | None,
    ) -> dict[str, int]:
        if not items:
            return {}
        if isinstance(items, dict):
            pairs = list(items.items())
        else:
            pairs = list(items)
        lines: dict[str, int] = {}
        for raw_sku, raw_qty in pairs:
            sku = str(raw_sku).strip()
            if not sku:
                raise ValidationError("备件编码不能为空")
            qty = raw_qty
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                raise ValidationError(f"备件 {sku} 数量必须为正整数")
            if sku in lines:
                raise ValidationError(f"备件行重复: {sku}")
            lines[sku] = qty
        return lines

    def _require_role(self, actor_id: str, roles: Iterable[str]) -> dict:
        row = self.db.query_one("SELECT * FROM users WHERE id = ?", (actor_id,))
        if row is None:
            raise PermissionDenied(f"用户不存在: {actor_id}")
        if row["active"] != 1:
            raise PermissionDenied(f"用户已停用: {actor_id}")
        if row["role"] not in roles:
            raise PermissionDenied(
                f"角色 {row['role']} 无权执行该动作，需要 {'/'.join(roles)}")
        return dict(row)

    def _require_owner_or_manager(self, actor_id: str, wo: sqlite3.Row) -> None:
        """负责人本人或管理员；维修动作还要求负责人是维修/管理员角色。"""

        actor = self.db.query_one("SELECT * FROM users WHERE id = ?", (actor_id,))
        if actor is None or actor["active"] != 1:
            raise PermissionDenied(f"用户不存在或已停用: {actor_id}")
        if actor["role"] == MANAGER:
            return
        if actor_id != wo["owner_id"]:
            raise PermissionDenied("只有工单负责人或管理员可以执行该动作")
        if actor["role"] not in (REPAIR, MANAGER):
            raise PermissionDenied("工单负责人必须具备维修角色才能执行该动作")

    @staticmethod
    def _exists(conn: sqlite3.Connection, table: str, entity_id: str, label: str) -> sqlite3.Row:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"{label}不存在: {entity_id}")
        return row

    @staticmethod
    def _lock_part(conn: sqlite3.Connection, sku: str) -> sqlite3.Row:
        """锁定备件行（写事务内即行锁）并返回当前行。"""

        row = conn.execute("SELECT * FROM parts WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise NotFoundError(f"备件不存在: {sku}")
        return row

    @staticmethod
    def _lock_door(conn: sqlite3.Connection, door_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM doors WHERE id = ?", (door_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"门不存在: {door_id}")
        return row

    @staticmethod
    def _lock_wo(conn: sqlite3.Connection, wo_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"工单不存在: {wo_id}")
        return row

    @staticmethod
    def _require_editable(wo: sqlite3.Row) -> None:
        if wo["status"] not in EDITABLE_STATES:
            raise ConflictError(
                f"工单状态 {wo['status']} 不允许再领料（完工/废单/关单后账目冻结）")

    @staticmethod
    def _release_remaining(conn: sqlite3.Connection, wo_id: str) -> int:
        """释放工单上全部仍处于预占状态（未领用）的数量。"""

        total = 0
        rows = conn.execute(
            "SELECT sku, reserved_qty FROM wo_parts WHERE wo_id = ? AND reserved_qty > 0",
            (wo_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE parts SET reserved_qty = reserved_qty - ? WHERE sku = ?",
                (row["reserved_qty"], row["sku"]),
            )
            conn.execute(
                "UPDATE wo_parts SET reserved_qty = 0 WHERE wo_id = ? AND sku = ?",
                (wo_id, row["sku"]),
            )
            total += row["reserved_qty"]
        return total

    def _allocate_code(self, conn: sqlite3.Connection, now: float) -> str:
        """生成业务工单号 ``WO-YYYYMMDD-序号``（按日序号，事务内串行无并发洞）。"""

        import time

        day = time.strftime("%Y%m%d", time.gmtime(now))
        prefix = f"WO-{day}-"
        count = conn.execute(
            "SELECT COUNT(*) FROM work_orders WHERE code LIKE ?", (prefix + "%",)
        ).fetchone()[0]
        return f"{prefix}{count + 1:04d}"

    def _next_event_seq(self, conn: sqlite3.Connection, wo_id: str) -> int:
        return conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM wo_events WHERE wo_id = ?", (wo_id,)
        ).fetchone()[0]

    def _event(
        self, conn: sqlite3.Connection, wo_id: str, kind: str, actor_id: str, at: float,
        **payload: Any,
    ) -> None:
        seq = self._next_event_seq(conn, wo_id)
        conn.execute(
            "INSERT INTO wo_events(id, seq, wo_id, kind, actor_id, at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_new_id("evt"), seq, wo_id, kind, actor_id, at,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def _audit(
        self, conn: sqlite3.Connection, kind: str, actor_id: str, **detail: Any
    ) -> None:
        conn.execute(
            "INSERT INTO audit_entries(id, kind, actor_id, at, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (_new_id("aud"), kind, actor_id, self._now(),
             json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)),
        )

    def _ledger(
        self,
        conn: sqlite3.Connection,
        sku: str,
        kind: str,
        qty: int,
        balance_after: int,
        actor_id: str,
        at: float,
        *,
        wo_id: str | None = None,
        ref: str = "",
        note: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO inventory_ledger(id, sku, kind, qty, balance_after, wo_id,"
            " actor_id, at, ref, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id("led"), sku, kind, qty, balance_after, wo_id, actor_id, at, ref, note),
        )

    @staticmethod
    def _manager_id(conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            "SELECT id FROM users WHERE role = 'manager' AND active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    @staticmethod
    def _reminder_note(wo: sqlite3.Row, kind: str, level: int) -> str:
        if kind == "due":
            base = f"工单 {wo['code']} 已超过 SLA 未完工，门仍处于维修锁定"
        else:
            base = f"工单 {wo['code']} 已完工但长期未关单，门仍被锁定，影响运营"
        return base if level == 1 else base + "（已升级至管理员）"

    @staticmethod
    def _part_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["available"] = data["on_hand"] - data["reserved_qty"]
        return data

    @staticmethod
    def _door_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["locked"] = bool(data["locked"])
        return data

    def _wo_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        data["overdue"] = (
            data["status"] in ("open", "in_progress") and self._now() > data["due_at"]
        )
        return data

    @staticmethod
    def _audit_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        return item


class _SystemClock:
    """默认时钟的最小实现（与 :class:`psdline.clock.SystemClock` 同形，避免循环导入）。"""

    @staticmethod
    def now() -> float:
        import time

        return time.time()
