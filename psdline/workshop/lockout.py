"""门区锁定投影。

锁定语义：门区在工单开单的同一事务里落 ``DOOR_LOCKED``，关单或废单的同一事务
里落 ``DOOR_UNLOCKED``。因为两个事件要么一起落盘要么都不落（注册表的提交是
原子的），"门被锁着却没有活动工单"在流水层面不可能出现。

活动锁状态直接从工单投影的门区活动索引派生；``DOOR_LOCKED/UNLOCKED`` 事件
本身作为物理锁动作的审计轨迹保留，可按门区回放。
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import WorkshopEvent


@dataclass(frozen=True)
class LockView:
    """一个门区当前的锁定状态。"""

    door_zone: str
    locked: bool
    order_id: str = ""
    since: float = 0.0


def active_locks(order_state: dict) -> dict[str, LockView]:
    """从工单投影派生全部活动锁：有活动工单即锁定。"""

    locks: dict[str, LockView] = {}
    for zone, order_id in order_state.get("active_by_zone", {}).items():
        order = order_state["orders"][order_id]
        locks[zone] = LockView(
            door_zone=zone,
            locked=True,
            order_id=order_id,
            since=order["opened_at"],
        )
    return locks


def is_locked(order_state: dict, door_zone: str) -> bool:
    return door_zone in order_state.get("active_by_zone", {})


def lock_history(events: list[WorkshopEvent], door_zone: str) -> list[dict]:
    """按门区回放物理锁动作（审计用，只读）。"""

    result = []
    for event in events:
        if event.kind not in ("DOOR_LOCKED", "DOOR_UNLOCKED"):
            continue
        if str(event.data.get("door_zone", "")) != door_zone:
            continue
        result.append(
            {
                "seq": event.seq,
                "kind": event.kind,
                "at": event.at,
                "actor": event.actor,
                "order_id": str(event.data.get("order_id", "")),
            }
        )
    return result
