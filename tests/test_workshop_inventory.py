"""备件库存：预占/实扣/退料/核销/盘点与台账恒等式。"""

from __future__ import annotations

import pytest

from psdline.workshop import InventoryShortage, PermissionDenied, StockDrift


def _seal(service) -> dict:
    return service.stock_levels()["SEAL-08"]


def _motor(service) -> dict:
    return service.stock_levels()["MOTOR-01"]


def test_reserve_does_not_touch_on_hand(workshop):
    workshop.open_order(
        "tech-day", "Z1", "x", [{"part_code": "MOTOR-01", "qty": 2}]
    )
    assert _motor(workshop)["on_hand"] == 2
    assert _motor(workshop)["reserved"] == 2
    assert _motor(workshop)["available"] == 0


def test_reserve_shortage_rejects_entire_order(workshop):
    with pytest.raises(InventoryShortage):
        workshop.open_order(
            "tech-day",
            "Z1",
            "x",
            [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 99}],
        )
    # 电机的预占也不能留下
    assert _motor(workshop)["reserved"] == 0
    assert _seal(workshop)["reserved"] == 0
    assert "Z1" not in workshop.active_locks()


def test_issue_batch_is_atomic(workshop):
    order_id = workshop.open_order(
        "tech-day",
        "Z1",
        "x",
        [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 2}],
    )
    # 密封圈超额：整批回滚，电机不能先出
    with pytest.raises(InventoryShortage):
        workshop.issue_parts(
            "store-1",
            order_id,
            [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 5}],
        )
    assert _motor(workshop)["on_hand"] == 2
    assert _motor(workshop)["issued_total"] == 0
    assert _seal(workshop)["issued_total"] == 0


def test_issue_debits_on_hand_and_reserved(workshop):
    order_id = workshop.open_order(
        "tech-day", "Z1", "x", [{"part_code": "SEAL-08", "qty": 4}]
    )
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 3}])
    seal = _seal(workshop)
    assert seal["on_hand"] == 7
    assert seal["reserved"] == 1
    assert seal["issued_total"] == 3
    assert seal["available"] == 6


def test_close_auto_releases_unissued(workshop):
    order_id = workshop.open_order(
        "tech-day", "Z1", "x", [{"part_code": "SEAL-08", "qty": 4}]
    )
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 3}])
    workshop.mark_repaired("tech-day", order_id)
    workshop.close_order("sup-1", order_id)
    seal = _seal(workshop)
    # 领 3 消耗，未领 1 自动回补：在架 7、预占 0、可分配 7
    assert seal["on_hand"] == 7
    assert seal["reserved"] == 0
    assert seal["available"] == 7


def test_void_requires_clearing_issued_parts(workshop):
    order_id = workshop.open_order(
        "tech-day", "Z1", "x", [{"part_code": "SEAL-08", "qty": 3}]
    )
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 3}])
    with pytest.raises(Exception, match="已领未清"):  # noqa: B017
        workshop.void_order("sup-1", order_id, "误报")


def test_return_then_void_restores_stock(workshop):
    order_id = workshop.open_order(
        "tech-day", "Z1", "x", [{"part_code": "SEAL-08", "qty": 3}]
    )
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 3}])
    # 退回 2 个实物
    workshop.return_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 2}], reason="未使用")
    seal = _seal(workshop)
    assert seal["on_hand"] == 9
    assert seal["returned_total"] == 2
    # 还剩 1 件已领未清，仍不能废
    with pytest.raises(Exception, match="已领未清"):  # noqa: B017
        workshop.void_order("sup-1", order_id, "误报")
    # 主管核销最后 1 件（必须给原因，库存不回补）
    with pytest.raises(Exception):  # noqa: B017
        workshop.write_off_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 1}], "装别门了")
    with pytest.raises(Exception):  # noqa: B017 - 核销必须有原因
        workshop.write_off_parts("sup-1", order_id, [{"part_code": "SEAL-08", "qty": 1}], " ")
    workshop.write_off_parts(
        "sup-1", order_id, [{"part_code": "SEAL-08", "qty": 1}], "已装到 2 号门"
    )
    workshop.void_order("sup-1", order_id, "误报")
    seal = _seal(workshop)
    # 初始 10 - 出库 3 + 退回 2 = 9；核销 1 件不回补
    assert seal["on_hand"] == 9
    assert seal["reserved"] == 0
    assert seal["written_off_total"] == 1
    assert workshop.get_order(order_id).status == "VOIDED"


def test_return_cannot_exceed_outstanding(workshop):
    order_id = workshop.open_order("tech-day", "Z1", "x", [{"part_code": "SEAL-08", "qty": 2}])
    workshop.issue_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 2}])
    workshop.return_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 2}])
    with pytest.raises(Exception):  # noqa: B017
        workshop.return_parts("store-1", order_id, [{"part_code": "SEAL-08", "qty": 1}])


def test_two_orders_split_limited_stock(workshop):
    # 第一单预占 6 个密封圈，第二单最多只能拿 4 个
    first = workshop.open_order("tech-day", "Z1", "x", [{"part_code": "SEAL-08", "qty": 6}])
    second = workshop.open_order("tech-day", "Z2", "y", [{"part_code": "SEAL-08", "qty": 4}])
    with pytest.raises(InventoryShortage):
        workshop.open_order("tech-day", "Z3", "z", [{"part_code": "SEAL-08", "qty": 1}])
    # 第一单领料后，第二单照常能领到自己预占的量
    workshop.issue_parts("store-1", first, [{"part_code": "SEAL-08", "qty": 6}])
    workshop.issue_parts("store-1", second, [{"part_code": "SEAL-08", "qty": 4}])
    assert _seal(workshop)["on_hand"] == 0


def test_recount_requires_supervisor_approval(workshop):
    with pytest.raises(PermissionDenied):
        workshop.recount("store-1", {"SEAL-08": 9}, note="少一件")
    result = workshop.recount("sup-1", {"SEAL-08": 9}, note="损坏丢弃")
    assert result["adjusted"] == [
        {"part_code": "SEAL-08", "delta": -1, "measured": 9, "book": 10}
    ]
    assert _seal(workshop)["on_hand"] == 9
    assert _seal(workshop)["adjust_total"] == -1
    # 无差异不落事件
    again = workshop.recount("sup-1", {"SEAL-08": 9})
    assert again["adjusted"] == []


def test_recount_cannot_drive_stock_negative_or_below_reserved(workshop):
    workshop.open_order("tech-day", "Z1", "x", [{"part_code": "SEAL-08", "qty": 8}])
    # 预占 8 后，调账后在架不得低于 8
    with pytest.raises(StockDrift):
        workshop.recount("sup-1", {"SEAL-08": 7})


def test_ledger_identity_holds_through_mixed_history(workshop):
    """复杂操作序列后恒等式 on_hand = registered - issued + returned + adjust。"""

    a = workshop.open_order("tech-day", "Z1", "a", [{"part_code": "SEAL-08", "qty": 5}])
    b = workshop.open_order("tech-day", "Z2", "b", [{"part_code": "SEAL-08", "qty": 3}])
    workshop.issue_parts("store-1", a, [{"part_code": "SEAL-08", "qty": 4}])
    workshop.issue_parts("store-1", b, [{"part_code": "SEAL-08", "qty": 3}])
    workshop.return_parts("store-1", a, [{"part_code": "SEAL-08", "qty": 1}])
    workshop.mark_repaired("tech-day", b)
    workshop.close_order("sup-1", b)  # b 全部领走，无释放
    workshop.recount("sup-1", {"SEAL-08": 3}, note="盘亏 1")
    assert workshop.verify_stock() == []
    seal = _seal(workshop)
    assert seal["on_hand"] == 10 - 7 + 1 - 1
    assert seal["reserved"] == 1  # 单 a 未领的 1 个
