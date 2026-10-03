"""并发领料：N 线程抢有限库存，总量守恒、无超发、无半批。"""

from __future__ import annotations

import concurrent.futures

import pytest

from psdline.workshop import InventoryShortage, WorkshopService
from psdline.clock import ManualClock
from psdline.store import JsonStore, JsonlJournal


def _service(tmp_path) -> WorkshopService:
    clock = ManualClock()
    return WorkshopService(
        JsonStore(str(tmp_path)),
        JsonlJournal(str(tmp_path), "workshop", segment_limit=128),
        clock=clock,
    )


def test_concurrent_issue_against_single_order(tmp_path):
    """10 个密封垫预占，8 个仓管线程各要领 2 个：恰好 5 个成功，总数守恒。"""

    service = _service(tmp_path)
    service.register_person("sup-1", "sup-1", "主管", "SUPERVISOR")
    service.register_person("sup-1", "tech-1", "技师", "TECH")
    for index in range(8):
        service.register_person("sup-1", f"st-{index}", f"仓管{index}", "STORE")
    service.register_part("sup-1", "GASKET", "密封垫", 10)
    order_id = service.open_order("tech-1", "Z1", "换垫", [{"part_code": "GASKET", "qty": 10}])

    outcomes: list[bool] = []

    def attempt(actor: str) -> bool:
        try:
            service.issue_parts(actor, order_id, [{"part_code": "GASKET", "qty": 2}])
            return True
        except InventoryShortage:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(attempt, f"st-{index}") for index in range(8)]
        outcomes = [future.result() for future in futures]

    assert sum(outcomes) == 5
    gasket = service.stock_levels()["GASKET"]
    assert gasket["issued_total"] == 10
    assert gasket["on_hand"] == 0
    assert gasket["reserved"] == 0
    # 流水投影重放结果与在线状态一致
    assert service.verify_stock() == []


def test_concurrent_open_same_zone_one_winner(tmp_path):
    service = _service(tmp_path)
    service.register_person("sup-1", "sup-1", "主管", "SUPERVISOR")
    for index in range(6):
        service.register_person("sup-1", f"t-{index}", f"技师{index}", "TECH")
    service.register_part("sup-1", "P", "通用件", 100)

    winners: list[str] = []
    errors: list[str] = []

    def attempt(index: int) -> None:
        try:
            order_id = service.open_order(
                f"t-{index}", "ZONE-X", f"报修{index}", [{"part_code": "P", "qty": 1}]
            )
            winners.append(order_id)
        except Exception as error:  # noqa: BLE001
            errors.append(str(error))

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(attempt, range(6)))

    assert len(winners) == 1
    assert len(errors) == 5
    assert len(service.active_locks()) == 1


def test_concurrent_reserve_limited_stock_never_oversells(tmp_path):
    """20 个线程对 10 件库存各预占 1 件：恰好 10 张单成功。"""

    service = _service(tmp_path)
    service.register_person("sup-1", "sup-1", "主管", "SUPERVISOR")
    for index in range(20):
        service.register_person("sup-1", f"t-{index}", f"技师{index}", "TECH")
    service.register_part("sup-1", "P", "稀有件", 10)

    opened: list[str] = []

    def attempt(index: int) -> None:
        try:
            order_id = service.open_order(
                f"t-{index}", f"Z-{index}", "x", [{"part_code": "P", "qty": 1}]
            )
            opened.append(order_id)
        except InventoryShortage:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(attempt, range(20)))

    assert len(opened) == 10
    stock = service.stock_levels()["P"]
    assert stock["reserved"] == 10
    assert stock["available"] == 0
    assert service.verify_stock() == []
