"""车间服务门面：权限校验 + 工作流编排。

所有用例都遵循同一形态：查当前状态 → 校验权限与业务条件 → 翻译成候选事件 →
交给注册表原子提交。服务层不持有任何可变状态，因此跨班、并发调用看到的永远
是注册表提交后的最新投影。

取舍说明（与流水事件一一对应，便于审计回看）：

* 备件**开单预占**：开单时逐行锁定 ``reserved``，别人开单抢不到同一批件；
  但实物在架数不变——库管盘点时货架上的件还在，只是已有归属。
* **领料实扣**：跨班领料（白班开单、夜班来领）记在同一张工单上，部分领料
  允许多次，但每次提交一个原子批，批里任意一件不够/超额，整批回滚。
* 关单：未领预占**自动回补**（ORDER_CLOSED.released），已领即视为消耗。
* 废单：未领预占同样**自动回补**；已领件必须先退库（实物回架）或主管核销
  （件确实用掉/弄丢，给出原因），全部清零才允许废单——**人工确认**而不是
  自动回补，因为件可能已经装到了别的门上，系统无法替人断定它在哪。
* 门区锁：开单同事务落 DOOR_LOCKED，关单/废单同事务落 DOOR_UNLOCKED。
"""

from __future__ import annotations

from typing import Callable

from ..clock import Clock, SystemClock
from ..store import JsonStore, JsonlJournal
from . import inventory, lockout, orders, reminders
from .errors import (
    InvalidTransition,
    InventoryShortage,
    OrderClosed,
    PermissionDenied,
    UnknownPerson,
    WorkshopError,
)
from .models import (
    ROLE_DISPATCH,
    ROLE_STORE,
    ROLE_SUPERVISOR,
    ROLE_TECH,
    Part,
    Person,
    WorkOrder,
)
from .registry import WorkshopRegistry, WorkshopView
from .reminders import LEVEL_AUDIENCE, ReminderPolicy

#: 各类动作允许的角色集合。
CAN_OPEN = frozenset({ROLE_TECH, ROLE_DISPATCH, ROLE_SUPERVISOR})
CAN_ISSUE = frozenset({ROLE_STORE, ROLE_SUPERVISOR})
CAN_RETURN_RECEIVE = frozenset({ROLE_STORE, ROLE_SUPERVISOR})
CAN_REPAIR = frozenset({ROLE_TECH, ROLE_SUPERVISOR})
CAN_CLOSE = frozenset({ROLE_SUPERVISOR})
CAN_VOID = frozenset({ROLE_SUPERVISOR})
CAN_WRITEOFF = frozenset({ROLE_SUPERVISOR})
CAN_REGISTER_PERSON = frozenset({ROLE_SUPERVISOR})
CAN_RECOUNT = frozenset({ROLE_STORE, ROLE_SUPERVISOR})
CAN_ADJUST = frozenset({ROLE_SUPERVISOR})

DEFAULT_SLA_HOURS = 24.0
_HOUR = 3600.0


class WorkshopService:
    """对外门面。"""

    def __init__(
        self,
        store: JsonStore,
        journal: JsonlJournal,
        clock: Clock | None = None,
        notifier: Callable[["reminders.WorkshopEvent"], None] | None = None,
        policy: ReminderPolicy | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.registry = WorkshopRegistry(store, journal, self.clock, notifier)
        self.policy = policy or ReminderPolicy()

    # ---- 内部工具 -----------------------------------------------------

    def _person(self, view: WorkshopView, actor_id: str) -> Person:
        person = view.person(actor_id)
        if person is None:
            raise UnknownPerson(f"人员未登记: {actor_id}")
        return person

    def _require_role(self, view: WorkshopView, actor_id: str, allowed: frozenset, action: str) -> Person:
        person = self._person(view, actor_id)
        if person.role not in allowed:
            raise PermissionDenied(
                f"{person.name}（{person.role}）无权执行“{action}”，需要角色 {'/'.join(sorted(allowed))}"
            )
        return person

    def _require_part(self, view: WorkshopView, code: str) -> Part:
        part = view.part(code)
        if part is None:
            raise InvalidTransition(f"备件未登记: {code}")
        return part

    def _load_open_order(self, view: WorkshopView, order_id: str) -> dict:
        order = view.order(order_id)
        if order is None:
            raise InvalidTransition(f"工单不存在: {order_id}")
        if orders.is_terminal(order):
            raise OrderClosed(f"工单 {order_id} 已终结（{order['status']}），历史不可变")
        return order

    # ---- 主数据 -------------------------------------------------------

    def register_person(self, actor_id: str, person_id: str, name: str, role: str, shift: str = "DAY") -> Person:
        """登记人员（仅主管）。首次建档允许自举（无任何人时）。"""

        def validator(view: WorkshopView) -> None:
            if view.people:
                self._require_role(view, actor_id, CAN_REGISTER_PERSON, "登记人员")

        event = self.registry.build_event(
            "PERSON_REGISTERED",
            actor_id,
            {"person_id": person_id, "name": name, "role": role, "shift": shift},
        )
        self.registry.commit([event], validator)
        return Person(id=person_id, name=name, role=role, shift=shift)

    def register_part(
        self,
        actor_id: str,
        code: str,
        name: str,
        initial_qty: int,
        unit: str = "个",
        location: str = "",
    ) -> None:
        """登记备件并录入初始在库（仓管或主管）。"""

        if initial_qty < 0:
            raise InvalidTransition("初始库存不能为负")

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_RECOUNT, "登记备件")
            if view.part(code) is not None:
                raise InvalidTransition(f"备件已登记: {code}")

        event = self.registry.build_event(
            "PART_REGISTERED",
            actor_id,
            {
                "part_code": code,
                "name": name,
                "unit": unit,
                "location": location,
                "qty": int(initial_qty),
            },
        )
        self.registry.commit([event], validator)

    def list_people(self) -> list[Person]:
        return list(self.registry.view_state()["people"].values())

    def list_parts(self) -> list[Part]:
        return list(self.registry.view_state()["parts"].values())

    def stock_levels(self) -> dict[str, dict]:
        """各备件当前数量快照（在架/预占/可分配及累计列）。"""

        stock = self.registry.view_state()["stock"]
        result = {}
        for code, entry in stock.items():
            row = dict(entry)
            row["available"] = entry["on_hand"] - entry["reserved"]
            result[code] = row
        return result

    # ---- 开单 ---------------------------------------------------------

    def open_order(
        self,
        actor_id: str,
        door_zone: str,
        title: str,
        lines: list[dict],
        owner_id: str = "",
        sla_hours: float = DEFAULT_SLA_HOURS,
    ) -> str:
        """门区报修开单：校验门区未锁、备件够预占，随后同事务上锁+预占。

        ``lines`` 形如 ``[{"part_code": "MOTOR-01", "qty": 1}, ...]``。允许空
        备件清单（纯人工检修单）。
        """

        if not door_zone:
            raise InvalidTransition("门区标识不能为空")
        normalized: list[dict] = []
        seen: set[str] = set()
        for raw in lines:
            code = str(raw["part_code"])
            qty = int(raw["qty"])
            if code in seen:
                raise InvalidTransition(f"工单内备件重复: {code}，请合并数量")
            seen.add(code)
            normalized.append({"part_code": code, "qty": qty})

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_OPEN, "开单")
            owner = self._person(view, owner_id or actor_id)
            if not owner:
                raise InvalidTransition("工单负责人未登记")
            for line in normalized:
                self._require_part(view, line["part_code"])
            if orders.active_for_zone(view.order_state, door_zone):
                raise InvalidTransition(
                    f"门区 {door_zone} 已被活动工单 "
                    f"{orders.active_for_zone(view.order_state, door_zone)} 锁定"
                )
            inventory.assert_can_reserve(view.stock, normalized)

        order_id = self.registry.ids.next()
        now = self.clock.now()
        opened = self.registry.build_event(
            "ORDER_OPENED",
            actor_id,
            {
                "order_id": order_id,
                "door_zone": door_zone,
                "title": title,
                "owner_id": owner_id or actor_id,
                "due_at": now + sla_hours * _HOUR,
                "lines": normalized,
            },
        )
        locked = self.registry.build_event(
            "DOOR_LOCKED",
            actor_id,
            {"door_zone": door_zone, "order_id": order_id, "reason": "报修开单"},
        )
        self.registry.commit([opened, locked], validator)
        return order_id

    # ---- 领料（跨班、分批、原子批）-----------------------------------

    def issue_parts(self, actor_id: str, order_id: str, moves: list[dict], handoff: str = "") -> None:
        """实物发料出库（仓管）。

        跨班安全：不校验操作人是不是开单人/本班人，只校验角色与数量。一个
        ``moves`` 批是原子的：任意一件超额或在架不足，整批回滚，已成功的行
        也不落账，杜绝"领了一半"。
        """

        normalized = self._normalize_moves(moves)

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_ISSUE, "发料")
            order = self._load_open_order(view, order_id)
            if not orders.is_issuable(order):
                raise InvalidTransition(f"工单 {order_id} 状态 {order['status']}，不可领料")
            capacities = orders.issue_capacity(order)
            wanted: dict[str, int] = {}
            for move in normalized:
                wanted[move["part_code"]] = wanted.get(move["part_code"], 0) + move["qty"]
            over = []
            for code, qty in wanted.items():
                if code not in capacities:
                    over.append(f"{code}: 不在工单清单上")
                elif qty > capacities[code]:
                    over.append(f"{code}: 领 {qty} 超过预占余量 {capacities[code]}")
            if over:
                raise InventoryShortage("领料超出工单预占: " + "；".join(over))
            inventory.assert_can_issue(view.stock, [{"part_code": c, "qty": q} for c, q in wanted.items()])

        event = self.registry.build_event(
            "PARTS_ISSUED",
            actor_id,
            {"order_id": order_id, "moves": normalized, "handoff": handoff},
        )
        self.registry.commit([event], validator)

    def return_parts(self, actor_id: str, order_id: str, moves: list[dict], reason: str = "") -> None:
        """退料：已领件实物退回货架（仓管签收）。废单前由主管组织退料。"""

        normalized = self._normalize_moves(moves)

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_RETURN_RECEIVE, "退料签收")
            order = self._load_open_order(view, order_id)
            pending = orders.outstanding_issued(order)
            over = []
            wanted: dict[str, int] = {}
            for move in normalized:
                wanted[move["part_code"]] = wanted.get(move["part_code"], 0) + move["qty"]
            for code, qty in wanted.items():
                if qty > pending.get(code, 0):
                    over.append(f"{code}: 退 {qty} 超过该单已领未清 {pending.get(code, 0)}")
            if over:
                raise InventoryShortage("退料超出已领未清: " + "；".join(over))

        event = self.registry.build_event(
            "PARTS_RETURNED",
            actor_id,
            {"order_id": order_id, "moves": normalized, "reason": reason[:300]},
        )
        self.registry.commit([event], validator)

    def write_off_parts(self, actor_id: str, order_id: str, moves: list[dict], reason: str) -> None:
        """主管核销已领件（件无法退回实物），必须给原因。库存不回补。"""

        if not reason.strip():
            raise InvalidTransition("核销必须填写原因，不能静默吞掉已领件")
        normalized = self._normalize_moves(moves)

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_WRITEOFF, "备件核销")
            order = self._load_open_order(view, order_id)
            pending = orders.outstanding_issued(order)
            over = []
            wanted: dict[str, int] = {}
            for move in normalized:
                wanted[move["part_code"]] = wanted.get(move["part_code"], 0) + move["qty"]
            for code, qty in wanted.items():
                if qty > pending.get(code, 0):
                    over.append(f"{code}: 核销 {qty} 超过该单已领未清 {pending.get(code, 0)}")
            if over:
                raise InventoryShortage("核销超出已领未清: " + "；".join(over))

        event = self.registry.build_event(
            "PARTS_WRITTEN_OFF",
            actor_id,
            {"order_id": order_id, "moves": normalized, "reason": reason[:300]},
        )
        self.registry.commit([event], validator)

    @staticmethod
    def _normalize_moves(moves: list[dict]) -> list[dict]:
        if not moves:
            raise InvalidTransition("备件动作清单不能为空")
        normalized: list[dict] = []
        for raw in moves:
            qty = int(raw["qty"])
            if qty <= 0:
                raise InvalidTransition(f"备件数量必须为正数: {raw}")
            normalized.append({"part_code": str(raw["part_code"]), "qty": qty})
        return normalized

    # ---- 报修完工 / 关单 / 废单 --------------------------------------

    def mark_repaired(self, actor_id: str, order_id: str, note: str = "") -> None:
        """技师报修完工（完工后仍允许补领料）。"""

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_REPAIR, "报修完工")
            order = self._load_open_order(view, order_id)
            if order["status"] != "OPEN":
                raise InvalidTransition(f"工单 {order_id} 已标记完工，无需重复")

        event = self.registry.build_event(
            "ORDER_REPAIRED", actor_id, {"order_id": order_id, "note": note[:300]}
        )
        self.registry.commit([event], validator)

    def close_order(self, actor_id: str, order_id: str, note: str = "") -> None:
        """主管关单：未领预占自动回补、门区解锁。已领件视为正常消耗。"""

        def build(view: WorkshopView):
            order_dict = self._load_open_order(view, order_id)
            if order_dict["status"] != "REPAIRED":
                raise InvalidTransition(
                    f"工单 {order_id} 尚未报修完工（当前 {order_dict['status']}），不能直接关单"
                )
            released = orders.unissued_reserved(order_dict)
            closed = self.registry.build_event(
                "ORDER_CLOSED",
                actor_id,
                {"order_id": order_id, "note": note[:300], "released": released},
            )
            unlocked = self.registry.build_event(
                "DOOR_UNLOCKED",
                actor_id,
                {"door_zone": order_dict["door_zone"], "order_id": order_id},
            )
            return [closed, unlocked]

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_CLOSE, "关单")
            self._load_open_order(view, order_id)

        self.registry.commit(build, validator)

    def void_order(self, actor_id: str, order_id: str, reason: str) -> None:
        """主管废单。

        前置：所有已领件必须已退库或已核销（人工确认流向）。未领预占由系统
        在同事务自动回补，门区解锁。
        """

        if not reason.strip():
            raise InvalidTransition("废单必须填写原因")

        def build(view: WorkshopView):
            order_dict = self._load_open_order(view, order_id)
            pending = orders.outstanding_issued(order_dict)
            if pending:
                detail = "、".join(f"{code} {qty}" for code, qty in sorted(pending.items()))
                raise InvalidTransition(
                    f"工单 {order_id} 还有已领未清备件（{detail}），请先退库或主管核销后再废单"
                )
            released = orders.unissued_reserved(order_dict)
            voided = self.registry.build_event(
                "ORDER_VOIDED",
                actor_id,
                {"order_id": order_id, "reason": reason[:300], "released": released},
            )
            unlocked = self.registry.build_event(
                "DOOR_UNLOCKED",
                actor_id,
                {"door_zone": order_dict["door_zone"], "order_id": order_id},
            )
            return [voided, unlocked]

        def validator(view: WorkshopView) -> None:
            self._require_role(view, actor_id, CAN_VOID, "废单")
            self._load_open_order(view, order_id)

        self.registry.commit(build, validator)

    # ---- 查询 ---------------------------------------------------------

    def get_order(self, order_id: str) -> WorkOrder:
        order = self.registry.get_order_view(order_id)
        if order is None:
            raise InvalidTransition(f"工单不存在: {order_id}")
        return order

    def list_orders(self, include_terminal: bool = True) -> list[WorkOrder]:
        orders_ = self.registry.list_order_views()
        if not include_terminal:
            orders_ = [item for item in orders_ if not item.terminal]
        return sorted(orders_, key=lambda item: item.opened_at)

    def active_locks(self) -> dict[str, lockout.LockView]:
        return lockout.active_locks(self.registry.view_state()["orders"])

    def lock_history(self, door_zone: str) -> list[dict]:
        return lockout.lock_history(self.registry.all_events(), door_zone)

    # ---- 超期提醒 -----------------------------------------------------

    def scan_overdue(self) -> list[dict]:
        """扫描活动工单，按分级策略补发提醒（同一级别只补一次）。

        返回本次新落盘的提醒摘要列表。通知对象由级别决定：L1 负责人本人，
        L2 加抄调度，L3 升级主管。
        """

        now = self.clock.now()
        people = self.registry.view_state()["people"]
        raised: list[dict] = []
        for order_dict in orders.active_orders(self.registry.view_state()["orders"]):
            order = orders.view(order_dict)
            last_level = self.registry.view_state()["reminders"].get(order.id, "")
            level = self.policy.evaluate(order, last_level, now)
            if not level:
                continue
            audience_ids = self._resolve_audience(level, order.owner_id, people)
            event = self.registry.build_event(
                "REMINDER_RAISED",
                "system",
                {
                    "order_id": order.id,
                    "door_zone": order.door_zone,
                    "level": level,
                    "owner_id": order.owner_id,
                    "audience": audience_ids,
                    "audience_roles": list(LEVEL_AUDIENCE[level]),
                    "overdue_seconds": self.policy.overdue_seconds(order, now),
                },
            )
            self.registry.commit([event])
            raised.append(
                {
                    "order_id": order.id,
                    "level": level,
                    "audience": audience_ids,
                    "door_zone": order.door_zone,
                }
            )
        return raised

    @staticmethod
    def _resolve_audience(level: str, owner_id: str, people: dict[str, Person]) -> list[str]:
        ids: list[str] = [owner_id] if owner_id else []
        role_set = set(LEVEL_AUDIENCE[level]) - {"owner"}
        for pid, person in people.items():
            if person.role == "SUPERVISOR" and "supervisor" in role_set:
                ids.append(pid)
            elif person.role == "DISPATCH" and "dispatch" in role_set:
                ids.append(pid)
        # 去重保序
        seen: set[str] = set()
        unique = []
        for pid in ids:
            if pid not in seen:
                seen.add(pid)
                unique.append(pid)
        return unique

    # ---- 盘点 / 对账恢复 ---------------------------------------------

    def recount(self, actor_id: str, counted: dict[str, int], note: str = "", require_approval: bool = True) -> dict:
        """实物盘点。

        ``counted`` 为件码 -> 实盘在架数。无差异不落任何事件；有差异必须主管
        批准（``require_approval``），随后以一条 STOCK_RECOUNTED 调整台账。
        调整后不得为负、不得低于已预占（否则 :class:`StockDrift`，整批回滚）。
        返回 ``{"adjusted": [...], "unchanged": [...]}``。
        """

        stock = self.registry.view_state()["stock"]
        adjustments: list[dict] = []
        unchanged: list[str] = []
        for code, measured in sorted(counted.items()):
            entry = stock.get(code)
            if entry is None:
                raise InvalidTransition(f"备件未登记，不能盘点: {code}")
            delta = int(measured) - entry["on_hand"]
            if delta == 0:
                unchanged.append(code)
            else:
                adjustments.append(
                    {"part_code": code, "delta": delta, "measured": int(measured), "book": entry["on_hand"]}
                )
        if not adjustments:
            return {"adjusted": [], "unchanged": unchanged}

        def validator(view: WorkshopView) -> None:
            self._require_role(
                view,
                actor_id,
                CAN_ADJUST if require_approval else CAN_RECOUNT,
                "盘亏盘盈调账",
            )
            inventory.assert_can_adjust(view.stock, adjustments)

        event = self.registry.build_event(
            "STOCK_RECOUNTED",
            actor_id,
            {"adjustments": adjustments, "note": note[:300]},
        )
        self.registry.commit([event], validator)
        return {"adjusted": adjustments, "unchanged": unchanged}

    def verify_stock(self) -> list[str]:
        """账实核对：快照台账 vs 流水全量重算。空列表代表账平。"""

        return self.registry.verify()

    def recover_stock(self, actor_id: str, note: str = "") -> list[str]:
        """账实不符恢复：以只追加流水为准重建台账（主管执行）。"""

        state = self.registry.view_state()
        if actor_id not in state["people"] or state["people"][actor_id].role != ROLE_SUPERVISOR:
            raise PermissionDenied("只有主管可以执行台账重建")
        return self.registry.rebuild_from_journal(actor_id, note)

