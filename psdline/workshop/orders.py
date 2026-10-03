"""工单生命周期投影。

工单状态只允许这条主链：

    OPEN ──repair──▶ REPAIRED ──close──▶ CLOSED
      │                  │
      └────────void──────┴────────void────▶ VOIDED

领料事件（``PARTS_ISSUED`` 等）不改变工单主状态，只更新行履约数量；活动态和
REPAIRED 态都能领料（报修验收时补件）。门区在开单时进入活动索引，关单/废单
时摘除，投影层据此提供 :func:`active_for_zone`，让"同一门区一张活动工单"的
联锁可被服务层直接校验。

投影只记账不判权；非法流转由 :class:`~psdline.workshop.service.WorkshopService`
在提交前拒绝（事件永远进不了流水）。
"""

from __future__ import annotations

from .events import WorkshopEvent
from .models import (
    ISSUABLE_STATUSES,
    ORDER_CLOSED,
    ORDER_OPEN,
    ORDER_REPAIRED,
    ORDER_VOIDED,
    OrderLine,
    TERMINAL_STATUSES,
    WorkOrder,
)

_HISTORY_KINDS = (
    "ORDER_OPENED",
    "PARTS_ISSUED",
    "PARTS_RETURNED",
    "PARTS_WRITTEN_OFF",
    "ORDER_REPAIRED",
    "ORDER_CLOSED",
    "ORDER_VOIDED",
    "REMINDER_RAISED",
)


def new_order_state() -> dict:
    """工单投影状态：工单表 + 门区活动索引。"""

    return {"orders": {}, "active_by_zone": {}}


def _blank_order(event: WorkshopEvent) -> dict:
    data = event.data
    return {
        "id": str(data["order_id"]),
        "door_zone": str(data["door_zone"]),
        "title": str(data.get("title", "")),
        "status": ORDER_OPEN,
        "opened_by": event.actor,
        "owner_id": str(data.get("owner_id", event.actor)),
        "opened_at": event.at,
        "due_at": float(data["due_at"]),
        "repaired_by": "",
        "repaired_at": 0.0,
        "closed_by": "",
        "closed_at": 0.0,
        "voided_by": "",
        "voided_at": 0.0,
        "void_reason": "",
        "close_note": "",
        "lines": {
            str(line["part_code"]): {
                "part_code": str(line["part_code"]),
                "reserved_qty": int(line["qty"]),
                "issued_qty": 0,
                "returned_qty": 0,
                "written_off_qty": 0,
            }
            for line in data.get("lines", [])
        },
        "history": [],
    }


def _note_history(order: dict, event: WorkshopEvent) -> None:
    if event.kind not in _HISTORY_KINDS:
        return
    order["history"].append(
        {
            "seq": event.seq,
            "kind": event.kind,
            "at": event.at,
            "actor": event.actor,
            "data": dict(event.data),
        }
    )


def apply(state: dict, event: WorkshopEvent) -> None:
    """把工单相关事件投影进工单表。"""

    if event.kind == "ORDER_OPENED":
        order = _blank_order(event)
        state["orders"][order["id"]] = order
        state["active_by_zone"][order["door_zone"]] = order["id"]
        _note_history(order, event)
        return

    order_id = str(event.data.get("order_id", ""))
    order = state["orders"].get(order_id)
    if order is None:
        # 门区事件不属于任何工单；其它未知工单事件不应发生（服务层已挡）。
        return

    if event.kind == "PARTS_ISSUED":
        for move in event.data.get("moves", []):
            line = order["lines"][str(move["part_code"])]
            line["issued_qty"] += int(move["qty"])
    elif event.kind == "PARTS_RETURNED":
        for move in event.data.get("moves", []):
            line = order["lines"][str(move["part_code"])]
            line["returned_qty"] += int(move["qty"])
    elif event.kind == "PARTS_WRITTEN_OFF":
        for move in event.data.get("moves", []):
            line = order["lines"][str(move["part_code"])]
            line["written_off_qty"] += int(move["qty"])
    elif event.kind == "ORDER_REPAIRED":
        order["status"] = ORDER_REPAIRED
        order["repaired_by"] = event.actor
        order["repaired_at"] = event.at
        if event.data.get("note"):
            order["close_note"] = str(event.data["note"])
    elif event.kind == "ORDER_CLOSED":
        order["status"] = ORDER_CLOSED
        order["closed_by"] = event.actor
        order["closed_at"] = event.at
        if event.data.get("note"):
            order["close_note"] = str(event.data["note"])
        state["active_by_zone"].pop(order["door_zone"], None)
    elif event.kind == "ORDER_VOIDED":
        order["status"] = ORDER_VOIDED
        order["voided_by"] = event.actor
        order["voided_at"] = event.at
        order["void_reason"] = str(event.data.get("reason", ""))
        state["active_by_zone"].pop(order["door_zone"], None)

    _note_history(order, event)


def get(state: dict, order_id: str) -> dict | None:
    """内部可变工单 dict（服务层校验使用）。"""

    return state["orders"].get(order_id)


def active_for_zone(state: dict, door_zone: str) -> str:
    """返回门区当前活动工单 id；没有则空串。"""

    return state["active_by_zone"].get(door_zone, "")


def active_orders(state: dict) -> list[dict]:
    """全部未终结工单（按到期时刻升序），超期扫描器使用。"""

    pending = [
        order
        for order in state["orders"].values()
        if order["status"] not in TERMINAL_STATUSES
    ]
    return sorted(pending, key=lambda item: (item["due_at"], item["opened_at"]))


def is_terminal(order: dict) -> bool:
    return order["status"] in TERMINAL_STATUSES


def is_issuable(order: dict) -> bool:
    return order["status"] in ISSUABLE_STATUSES


def outstanding_issued(order: dict) -> dict[str, int]:
    """已领未清（未退未核销）数量按件汇总——废单前必须全部为 0。"""

    result: dict[str, int] = {}
    for line in order["lines"].values():
        pending = line["issued_qty"] - line["returned_qty"] - line["written_off_qty"]
        if pending > 0:
            result[line["part_code"]] = pending
    return result


def unissued_reserved(order: dict) -> list[dict]:
    """终结时应自动释放的未领预占清单。"""

    return [
        {"part_code": line["part_code"], "qty": line["reserved_qty"] - line["issued_qty"]}
        for line in order["lines"].values()
        if line["reserved_qty"] - line["issued_qty"] > 0
    ]


def issue_capacity(order: dict) -> dict[str, int]:
    """该单每个件当前还能领多少（预占余量，供并发/跨班二次领校验）。"""

    return {
        line["part_code"]: line["reserved_qty"] - line["issued_qty"]
        for line in order["lines"].values()
    }


def view(order: dict) -> WorkOrder:
    """内部 dict 转不可变 :class:`WorkOrder` 视图。"""

    lines = tuple(
        OrderLine(
            part_code=line["part_code"],
            reserved_qty=line["reserved_qty"],
            issued_qty=line["issued_qty"],
            returned_qty=line["returned_qty"],
            written_off_qty=line["written_off_qty"],
        )
        for line in (order["lines"][code] for code in sorted(order["lines"]))
    )
    return WorkOrder(
        id=order["id"],
        door_zone=order["door_zone"],
        title=order["title"],
        status=order["status"],
        opened_by=order["opened_by"],
        owner_id=order["owner_id"],
        opened_at=order["opened_at"],
        due_at=order["due_at"],
        repaired_by=order["repaired_by"],
        repaired_at=order["repaired_at"],
        closed_by=order["closed_by"],
        closed_at=order["closed_at"],
        voided_by=order["voided_by"],
        voided_at=order["voided_at"],
        void_reason=order["void_reason"],
        close_note=order["close_note"],
        lines=lines,
        history=tuple(order["history"]),
    )


def views(state: dict) -> list[WorkOrder]:
    return [view(state["orders"][key]) for key in sorted(state["orders"])]


def rebuild(events: list[WorkshopEvent]) -> dict:
    """从空投影开始重放全部工单事件（恢复/对账用）。"""

    state = new_order_state()
    for event in events:
        apply(state, event)
    return state
