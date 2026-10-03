"""工单生命周期、门区锁定与权限。"""

from __future__ import annotations

import pytest

from psdline.workshop import (
    InvalidTransition,
    OrderClosed,
    PermissionDenied,
)

LINES = [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 4}]


def test_open_locks_zone_and_reserves(workshop):
    order_id = workshop.open_order("tech-day", "ZONE-A", "门关不上", LINES, owner_id="tech-day")
    order = workshop.get_order(order_id)

    assert order.status == "OPEN"
    assert workshop.active_locks()["ZONE-A"].order_id == order_id
    stock = workshop.stock_levels()
    assert stock["MOTOR-01"]["reserved"] == 1
    assert stock["MOTOR-01"]["available"] == 1
    # 预占不动实物在架
    assert stock["MOTOR-01"]["on_hand"] == 2
    kinds = [item["kind"] for item in order.history]
    assert kinds == ["ORDER_OPENED"]


def test_same_zone_cannot_have_two_active_orders(workshop):
    workshop.open_order("tech-day", "ZONE-A", "单 1", LINES)
    with pytest.raises(InvalidTransition, match="锁定"):
        workshop.open_order("tech-day", "ZONE-A", "单 2", [])


def test_zone_unlocks_only_on_terminal(workshop):
    order_id = workshop.open_order("tech-day", "ZONE-A", "x", LINES)
    workshop.issue_parts(
        "store-1", order_id, [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 4}]
    )
    workshop.mark_repaired("tech-day", order_id)
    # 完工但未关单，门仍然锁着
    assert "ZONE-A" in workshop.active_locks()
    workshop.close_order("sup-1", order_id)
    assert "ZONE-A" not in workshop.active_locks()
    history = [item["kind"] for item in workshop.lock_history("ZONE-A")]
    assert history == ["DOOR_LOCKED", "DOOR_UNLOCKED"]


def test_close_requires_repaired_and_supervisor(workshop):
    order_id = workshop.open_order("tech-day", "ZONE-A", "x", LINES)
    with pytest.raises(PermissionDenied):
        workshop.close_order("store-1", order_id)
    with pytest.raises(PermissionDenied):
        workshop.close_order("tech-day", order_id)
    with pytest.raises(InvalidTransition, match="尚未报修完工"):
        workshop.close_order("sup-1", order_id)
    workshop.mark_repaired("tech-day", order_id)
    workshop.close_order("sup-1", order_id, note="验收通过")
    assert workshop.get_order(order_id).status == "CLOSED"
    assert workshop.get_order(order_id).closed_by == "sup-1"


def test_dispatch_and_supervisor_may_open(workshop):
    assert workshop.open_order("disp-1", "Z1", "调度开单", [])
    assert workshop.open_order("sup-1", "Z2", "主管开单", [])


def test_technician_cannot_issue_storekeeper_cannot_repair(workshop):
    order_id = workshop.open_order("tech-day", "ZONE-A", "x", LINES)
    with pytest.raises(PermissionDenied):
        workshop.issue_parts("tech-day", order_id, [{"part_code": "MOTOR-01", "qty": 1}])
    with pytest.raises(PermissionDenied):
        workshop.mark_repaired("store-1", order_id)
    with pytest.raises(PermissionDenied):
        workshop.void_order("tech-day", order_id, "不要了")


def test_unknown_actor_rejected(workshop):
    with pytest.raises(Exception):  # noqa: B017 - UnknownPerson
        workshop.open_order("nobody", "ZONE-A", "x", LINES)


def test_terminal_order_is_frozen(workshop):
    order_id = workshop.open_order("tech-day", "ZONE-A", "x", LINES)
    workshop.mark_repaired("tech-day", order_id)
    workshop.close_order("sup-1", order_id)
    with pytest.raises(OrderClosed):
        workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 1}])
    with pytest.raises(OrderClosed):
        workshop.mark_repaired("tech-day", order_id)
    with pytest.raises(OrderClosed):
        workshop.void_order("sup-1", order_id, "晚了")
    # 历史条数与内容在终结后仍然完整可读
    kinds = [item["kind"] for item in workshop.get_order(order_id).history]
    assert kinds == ["ORDER_OPENED", "ORDER_REPAIRED", "ORDER_CLOSED"]


def test_cross_shift_issue_recorded_on_same_order(workshop):
    order_id = workshop.open_order(
        "tech-day", "ZONE-A", "白班开单", [{"part_code": "SEAL-08", "qty": 3}], owner_id="tech-day"
    )
    # 夜班技师不能发料，夜班仓管可以；谁发都记在同一张单
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 2}], handoff="白班->夜班")
    order = workshop.get_order(order_id)
    line = order.line("SEAL-08")
    assert line.issued_qty == 2
    assert line.unissued_qty == 1
    assert [item["actor"] for item in order.history if item["kind"] == "PARTS_ISSUED"] == ["store-1"]


def test_partial_issue_allowed_until_reserved_exhausted(workshop):
    order_id = workshop.open_order("tech-day", "Z", "x", [{"part_code": "SEAL-08", "qty": 3}])
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 2}])
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 1}])
    with pytest.raises(Exception):  # noqa: B017 - InventoryShortage
        workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 1}])


def test_issue_still_possible_after_repaired(workshop):
    order_id = workshop.open_order("tech-day", "Z", "x", [{"part_code": "SEAL-08", "qty": 2}])
    workshop.mark_repaired("tech-day", order_id)
    # 验收发现还要补一个件：REPAIRED 态仍可领料，领完再关单
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 2}])
    workshop.close_order("sup-1", order_id)
    assert workshop.get_order(order_id).status == "CLOSED"


def test_void_reason_required_and_unissued_released(workshop):
    order_id = workshop.open_order("tech-day", "Z", "x", [{"part_code": "SEAL-08", "qty": 3}])
    with pytest.raises(InvalidTransition):
        workshop.void_order("sup-1", order_id, "  ")
    workshop.void_order("sup-1", order_id, "误报修")
    assert workshop.stock_levels()["SEAL-08"]["reserved"] == 0
    assert workshop.get_order(order_id).status == "VOIDED"


def test_open_with_unregistered_part_rejected(workshop):
    with pytest.raises(InvalidTransition):
        workshop.open_order("tech-day", "Z", "x", [{"part_code": "NOPE", "qty": 1}])


def test_duplicate_line_in_one_order_rejected(workshop):
    with pytest.raises(InvalidTransition, match="重复"):
        workshop.open_order(
            "tech-day",
            "Z",
            "x",
            [{"part_code": "SEAL-08", "qty": 1}, {"part_code": "SEAL-08", "qty": 2}],
        )
