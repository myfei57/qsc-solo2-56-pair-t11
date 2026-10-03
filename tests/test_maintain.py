"""工单备件子系统测试。

并发用例（超卖/回滚）必须用**文件库**：内存库是单连接共享缓存，测不出
``BEGIN IMMEDIATE`` 在多连接下的串行化语义。
"""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import threading

import pytest

from psdline.clock import ManualClock
from psdline.maintain import (
    Database,
    MaintenanceApi,
    MaintenanceService,
    RecordingNotifier,
)
from psdline.maintain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)

U_REPORT = "u-report"
U_REPAIR = "u-repair"
U_STORE = "u-store"
U_ADMIN = "u-admin"


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture()
def svc(tmp_path, clock) -> MaintenanceService:
    return MaintenanceService(
        Database(str(tmp_path / "maintain.db")),
        clock=clock,
        notifier=RecordingNotifier(),
    )


def _open(svc, door="D-PSD-0101", parts=None, owner=U_REPAIR, **kwargs):
    return svc.open_work_order(
        U_REPORT, door, "门故障", parts, owner_id=owner, **kwargs
    )


# ---------------------------------------------------------------------------
# 开单与预占
# ---------------------------------------------------------------------------

def test_open_reserves_and_locks(svc):
    wo = _open(svc, parts={"MTR-DC110": 2, "BELT-1200": 3})
    assert wo["status"] == "open"
    assert wo["code"].startswith("WO-")

    door = svc.get_door("D-PSD-0101")
    assert door["locked"] is True
    assert door["active_wo"] == wo["id"]

    motor = svc.get_part("MTR-DC110")
    assert motor["on_hand"] == 5          # 实物没动
    assert motor["reserved_qty"] == 2     # 只冻结
    assert motor["available"] == 3


def test_open_rolls_back_when_any_part_insufficient(svc):
    with pytest.raises(ConflictError):
        _open(svc, parts={"MTR-DC110": 1, "BELT-1200": 999})
    # 整笔回滚：门没锁、电机没预占、工单不存在
    assert svc.get_door("D-PSD-0101")["locked"] is False
    assert svc.get_part("MTR-DC110")["reserved_qty"] == 0
    assert svc.list_work_orders() == []


def test_open_unknown_part_or_door(svc):
    with pytest.raises(NotFoundError):
        _open(svc, parts={"NOPE": 1})
    with pytest.raises(NotFoundError):
        _open(svc, door="D-NOPE")


def test_same_door_cannot_have_two_active_orders(svc):
    _open(svc)
    with pytest.raises(ConflictError):
        _open(svc, parts={"BELT-1200": 1})


def test_validation(svc):
    with pytest.raises(ValidationError):
        _open(svc, parts={"MTR-DC110": 0})
    with pytest.raises(ValidationError):
        _open(svc, parts=[("MTR-DC110", 1), ("MTR-DC110", 2)])  # 重复行
    with pytest.raises(ValidationError):
        svc.open_work_order(U_REPORT, "D-PSD-0101", "", {"BELT-1200": 1})


# ---------------------------------------------------------------------------
# 发料：并发、回滚、权限
# ---------------------------------------------------------------------------

def test_issue_deducts_stock_and_reservation(svc):
    wo = _open(svc, parts={"MTR-DC110": 2})
    svc.issue_parts(U_STORE, wo["id"], {"MTR-DC110": 2})
    motor = svc.get_part("MTR-DC110")
    assert motor["on_hand"] == 3
    assert motor["reserved_qty"] == 0
    line = svc.get_work_order(wo["id"], with_history=False)["parts"][0]
    assert line["issued_qty"] == 2
    ledger = svc.ledger("MTR-DC110")
    assert ledger[0]["kind"] == "issue"
    assert ledger[0]["qty"] == -2
    assert ledger[0]["balance_after"] == 3


def test_multi_line_issue_is_atomic(svc):
    wo = _open(svc, parts={"MTR-DC110": 1, "BELT-1200": 1})
    belt_before = svc.get_part("BELT-1200")["on_hand"]
    # 电机 1 台合法，再带 4 台超领 → 整笔失败
    with pytest.raises(ConflictError):
        svc.issue_parts(U_STORE, wo["id"], {"BELT-1200": 1, "MTR-DC110": 5})
    assert svc.get_part("BELT-1200")["on_hand"] == belt_before
    assert svc.get_part("MTR-DC110")["reserved_qty"] == 1
    line_motor = [p for p in svc.get_work_order(wo["id"], with_history=False)["parts"]
                  if p["sku"] == "MTR-DC110"][0]
    assert line_motor["issued_qty"] == 0


def test_concurrent_issues_never_oversell(tmp_path, clock):
    """两个线程同时对同一预占行发料，最多成功到计划量，库存绝不被扣成负数。"""

    svc = MaintenanceService(
        Database(str(tmp_path / "race.db")), clock=clock, notifier=RecordingNotifier()
    )
    wo = svc.open_work_order(U_REPORT, "D-PSD-0101", "并发",
                             {"BOLT-M8x30": 100}, owner_id=U_REPAIR)

    outcomes: list[str] = []

    def worker(qty: int) -> None:
        try:
            svc.issue_parts(U_STORE, wo["id"], {"BOLT-M8x30": qty})
            outcomes.append(f"ok-{qty}")
        except ConflictError:
            outcomes.append(f"fail-{qty}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, 20) for _ in range(8)]
        for f in futures:
            f.result()

    assert outcomes.count("ok-20") == 5
    assert outcomes.count("fail-20") == 3
    part = svc.get_part("BOLT-M8x30")
    assert part["on_hand"] == 100          # 200 - 100
    assert part["reserved_qty"] == 0
    assert svc.verify_consistency()["ok"] is True


def test_concurrent_receive_and_issue(tmp_path, clock):
    """入库与发料并发：流水重算必须始终等于投影（锁串行化的强校验）。"""

    svc = MaintenanceService(
        Database(str(tmp_path / "race2.db")), clock=clock, notifier=RecordingNotifier()
    )
    # 库存 200 螺栓，两张单各预占 100；再补 100 后最终应结存 100
    wo1 = svc.open_work_order(U_REPORT, "D-PSD-0101", "w1",
                              {"BOLT-M8x30": 100}, owner_id=U_REPAIR)
    wo2 = svc.open_work_order(U_REPORT, "D-PSD-0102", "w2",
                              {"BOLT-M8x30": 100}, owner_id=U_REPAIR)
    errors: list[Exception] = []

    def issue_retry(wo):
        for _ in range(100):
            try:
                svc.issue_parts(U_STORE, wo, {"BOLT-M8x30": 100})
                return
            except ConflictError:
                continue
        errors.append(RuntimeError("发料始终失败"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        fs = [pool.submit(lambda: svc.receive_stock(U_STORE, "BOLT-M8x30", 100)),
              pool.submit(issue_retry, wo1["id"]),
              pool.submit(issue_retry, wo2["id"])]
        for f in fs:
            f.result()

    assert errors == []
    assert svc.get_part("BOLT-M8x30")["on_hand"] == 100
    assert svc.verify_consistency()["ok"] is True


def test_issue_permission_denied(svc):
    wo = _open(svc, parts={"BELT-1200": 1})
    with pytest.raises(PermissionDenied):
        svc.issue_parts(U_REPORT, wo["id"], {"BELT-1200": 1})
    with pytest.raises(PermissionDenied):
        svc.issue_parts(U_REPAIR, wo["id"], {"BELT-1200": 1})
    with pytest.raises(PermissionDenied):
        svc.issue_parts("nobody", wo["id"], {"BELT-1200": 1})


def test_cannot_issue_after_finished(svc):
    wo = _open(svc, parts={"BELT-1200": 1})
    svc.complete_work(U_REPAIR, wo["id"], "没换件直接修好")
    with pytest.raises(ConflictError):
        svc.issue_parts(U_STORE, wo["id"], {"BELT-1200": 1})


# ---------------------------------------------------------------------------
# 状态机、锁定、关单权限
# ---------------------------------------------------------------------------

def test_complete_does_not_unlock_close_does(svc):
    wo = _open(svc, parts={"MTR-DC110": 1})
    svc.complete_work(U_REPAIR, wo["id"], "换件完成")
    assert svc.get_door("D-PSD-0101")["locked"] is True
    assert svc.get_part("MTR-DC110")["reserved_qty"] == 0  # 未领预占已释放
    assert svc.get_part("MTR-DC110")["on_hand"] == 5       # 没领料实物不动

    closed = svc.close_work_order(U_REPAIR, wo["id"])
    assert closed["status"] == "closed"
    assert svc.get_door("D-PSD-0101")["locked"] is False
    assert svc.get_door("D-PSD-0101")["active_wo"] is None


def test_reporter_cannot_close(svc):
    wo = _open(svc)
    with pytest.raises(PermissionDenied):
        svc.close_work_order(U_REPORT, wo["id"])


def test_manager_can_close_and_void(svc):
    wo = _open(svc)
    svc.close_work_order(U_ADMIN, wo["id"])
    assert svc.get_door("D-PSD-0101")["locked"] is False


def test_illegal_transitions(svc):
    wo = _open(svc)
    svc.close_work_order(U_REPAIR, wo["id"])
    with pytest.raises(ConflictError):
        svc.complete_work(U_REPAIR, wo["id"], "再完工")
    with pytest.raises(ConflictError):
        svc.close_work_order(U_REPAIR, wo["id"])
    with pytest.raises(ConflictError):
        svc.void_work_order(U_ADMIN, wo["id"], "再废一次")


def test_reassign_is_manager_only_and_audited(svc):
    wo = _open(svc, owner=U_REPAIR)
    with pytest.raises(PermissionDenied):
        svc.reassign(U_REPAIR, wo["id"], "u-repair2")
    svc.create_user(U_ADMIN, "u-repair2", "晚班维修", "repair")
    svc.reassign(U_ADMIN, wo["id"], "u-repair2", shift="晚班")
    assert svc.get_work_order(wo["id"], with_history=False)["owner_id"] == "u-repair2"
    kinds = [e["kind"] for e in svc.work_order_history(wo["id"])]
    assert "reassigned" in kinds


# ---------------------------------------------------------------------------
# 废单与人工退料
# ---------------------------------------------------------------------------

def test_void_releases_unissued_and_creates_return_task(svc):
    wo = _open(svc, parts={"MTR-DC110": 2, "BELT-1200": 3})
    svc.issue_parts(U_STORE, wo["id"], {"MTR-DC110": 2})
    motor_before = svc.get_part("MTR-DC110")["on_hand"]
    belt = svc.get_part("BELT-1200")

    svc.void_work_order(U_ADMIN, wo["id"], "误报")
    # 门解锁
    assert svc.get_door("D-PSD-0101")["locked"] is False
    # 皮带未领用：预占自动释放，实物不变
    assert svc.get_part("BELT-1200")["reserved_qty"] == 0
    assert svc.get_part("BELT-1200")["on_hand"] == belt["on_hand"]
    # 电机已领用：实物不自动回补，生成 2 件退料任务
    assert svc.get_part("MTR-DC110")["on_hand"] == motor_before
    tasks = svc.list_return_tasks()
    assert len(tasks) == 1 and tasks[0]["qty_out"] == 2


def test_return_is_manual_and_partial(svc):
    wo = _open(svc, parts={"MTR-DC110": 2})
    svc.issue_parts(U_STORE, wo["id"], {"MTR-DC110": 2})
    svc.void_work_order(U_ADMIN, wo["id"], "误报")
    task = svc.list_return_tasks()[0]

    before = svc.get_part("MTR-DC110")["on_hand"]
    svc.confirm_return(U_STORE, task["id"], 1)
    assert svc.get_part("MTR-DC110")["on_hand"] == before + 1
    partial = svc.get_return_task(task["id"])
    assert partial["status"] == "pending"

    # 超退被拒
    with pytest.raises(ConflictError):
        svc.confirm_return(U_STORE, task["id"], 5)
    # 报修人不能退料
    with pytest.raises(PermissionDenied):
        svc.confirm_return(U_REPORT, task["id"], 1)

    svc.confirm_return(U_STORE, task["id"], 1)
    assert svc.get_return_task(task["id"])["status"] == "completed"
    # 有退料流水
    assert svc.ledger("MTR-DC110")[0]["kind"] == "return"
    assert svc.verify_consistency()["ok"] is True


# ---------------------------------------------------------------------------
# 超期提醒
# ---------------------------------------------------------------------------

def test_overdue_reminder_levels_and_dedup(svc):
    wo = _open(svc)
    assert svc.scan_overdue() == []                 # 未到期
    svc.clock.advance(25 * 3600)
    first = svc.scan_overdue()
    assert len(first) == 1 and first[0]["level"] == 1
    assert first[0]["target_user_id"] == U_REPAIR  # 提醒负责人
    assert svc.scan_overdue() == []                # 去重
    svc.clock.advance(24 * 3600)
    second = svc.scan_overdue()
    assert len(second) == 1 and second[0]["level"] == 2
    assert second[0]["target_user_id"] == U_ADMIN  # 升级管理员
    # 关单后不再提醒
    svc.close_work_order(U_REPAIR, wo["id"])
    assert svc.scan_overdue() == []


def test_done_unclosed_reminder(svc):
    wo = _open(svc)
    svc.complete_work(U_REPAIR, wo["id"], "好了等验收")
    svc.clock.advance(25 * 3600)
    reminded = svc.scan_overdue()
    assert reminded[0]["kind"] == "done_unclosed"
    assert svc.get_door("D-PSD-0101")["locked"] is True


def test_reminder_follows_reassigned_owner(svc):
    """改派后：旧负责人已收过本级提醒，新负责人仍应收到同一级提醒。"""

    svc.create_user(U_ADMIN, "u-repair2", "晚班维修", "repair")
    wo = _open(svc)
    svc.clock.advance(25 * 3600)
    first = svc.scan_overdue()
    assert [r["target_user_id"] for r in first] == [U_REPAIR]

    svc.reassign(U_ADMIN, wo["id"], "u-repair2", shift="晚班")
    again = svc.scan_overdue()
    assert [r["target_user_id"] for r in again] == ["u-repair2"]
    # 再扫描一次：同一负责人不重复
    assert svc.scan_overdue() == []


# ---------------------------------------------------------------------------
# 盘点双人控制与账实恢复
# ---------------------------------------------------------------------------

def test_stocktake_requires_two_person(svc):
    take = svc.create_stocktake(U_STORE, "BELT-1200", 9, note="实点 9")
    with pytest.raises(PermissionDenied):
        svc.decide_stocktake(U_STORE, take["id"], True)
    with pytest.raises(PermissionDenied):
        svc.decide_stocktake(U_REPAIR, take["id"], True)
    approved = svc.decide_stocktake(U_ADMIN, take["id"], True)
    assert approved["status"] == "approved"
    assert svc.get_part("BELT-1200")["on_hand"] == 9
    ledger = svc.ledger("BELT-1200")
    assert ledger[0]["kind"] == "adjust"
    assert ledger[0]["qty"] == -3
    assert ledger[0]["balance_after"] == 9
    # 已处理的盘点单不能重复批准
    with pytest.raises(ConflictError):
        svc.decide_stocktake(U_ADMIN, take["id"], True)


def test_stocktake_reject_keeps_books(svc):
    take = svc.create_stocktake(U_STORE, "BELT-1200", 1)
    svc.decide_stocktake(U_ADMIN, take["id"], False)
    assert svc.get_part("BELT-1200")["on_hand"] == 12


def test_pending_stocktake_unique_per_sku(svc):
    svc.create_stocktake(U_STORE, "BELT-1200", 10)
    with pytest.raises(ConflictError):
        svc.create_stocktake(U_STORE, "BELT-1200", 11)


def test_verify_detects_and_rebuild_repairs_drift(svc):
    _open(svc, parts={"MTR-DC110": 2})
    assert svc.verify_consistency()["ok"] is True

    # 模拟「库存对不上」：有人绕过系统直接改了投影（实物丢失/脏写）
    conn = svc.db.connect()
    conn.execute("UPDATE parts SET on_hand = 2 WHERE sku = 'MTR-DC110'")
    conn.commit()
    report = svc.verify_consistency()
    assert not report["ok"]
    assert {v["kind"] for v in report["violations"]} >= {"stock_ledger_drift"}

    # 普通角色不能恢复
    with pytest.raises(PermissionDenied):
        svc.rebuild_projections(U_STORE)

    fixed = svc.rebuild_projections(U_ADMIN)
    assert fixed["fixed"]
    assert fixed["verify"]["ok"] is True
    # 流水才是真相：重建后回到流水结存 5（预占仍在）
    assert svc.get_part("MTR-DC110")["on_hand"] == 5
    assert svc.get_part("MTR-DC110")["reserved_qty"] == 2


def test_verify_detects_orphan_lock(svc):
    _open(svc)
    conn = svc.db.connect()
    conn.execute("UPDATE doors SET locked = 0, active_wo = NULL WHERE id = 'D-PSD-0101'")
    conn.commit()
    kinds = {v["kind"] for v in svc.verify_consistency()["violations"]}
    assert "active_wo_without_lock" in kinds
    rebuilt = svc.rebuild_projections(U_ADMIN)
    assert rebuilt["verify"]["ok"] is True
    assert svc.get_door("D-PSD-0101")["locked"] is True


# ---------------------------------------------------------------------------
# 不可变历史
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("table,id_col", [
    ("wo_events", "id"),
    ("inventory_ledger", "id"),
    ("audit_entries", "id"),
    ("reminders", "id"),
])
def test_history_tables_are_immutable(svc, table, id_col):
    wo = _open(svc, parts={"BELT-1200": 1})
    if table == "reminders":
        svc.clock.advance(25 * 3600)
        svc.scan_overdue()
    conn = svc.db.connect()
    row_id = conn.execute(f"SELECT {id_col} FROM {table} LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE {table} SET kind = 'x' WHERE {id_col} = ?", (row_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {table} WHERE {id_col} = ?", (row_id,))


def test_work_order_history_is_append_only_ordered(svc):
    wo = _open(svc, parts={"BELT-1200": 1})
    svc.issue_parts(U_STORE, wo["id"], {"BELT-1200": 1})
    svc.start_work(U_REPAIR, wo["id"])
    svc.complete_work(U_REPAIR, wo["id"], "done")
    svc.close_work_order(U_REPAIR, wo["id"])
    history = svc.work_order_history(wo["id"])
    assert [e["kind"] for e in history] == [
        "opened", "parts_issued", "started", "completed", "closed"
    ]
    assert [e["seq"] for e in history] == [1, 2, 3, 4, 5]
    # 事件载荷可读
    assert history[0]["payload"]["parts"] == {"BELT-1200": 1}


# ---------------------------------------------------------------------------
# 其它生命周期边角
# ---------------------------------------------------------------------------

def test_parts_lines_accept_tuples(svc):
    wo = _open(svc, parts=[("MTR-DC110", 1), ("BELT-1200", 1)])
    svc.issue_parts(U_STORE, wo["id"], [("MTR-DC110", 1)])
    line = [p for p in svc.get_work_order(wo["id"], with_history=False)["parts"]
            if p["sku"] == "MTR-DC110"][0]
    assert line["issued_qty"] == 1


def test_void_without_issued_parts_has_no_return_tasks(svc):
    wo = _open(svc, parts={"BELT-1200": 2})
    svc.void_work_order(U_ADMIN, wo["id"], "取消施工")
    assert svc.list_return_tasks() == []
    assert svc.get_part("BELT-1200")["reserved_qty"] == 0
    assert svc.get_part("BELT-1200")["on_hand"] == 12
    assert svc.get_door("D-PSD-0101")["locked"] is False


def test_close_open_directly_releases_reservation(svc):
    """免修直接关单：预占释放、门解锁、实物不动。"""

    wo = _open(svc, parts={"MTR-DC110": 1})
    svc.close_work_order(U_ADMIN, wo["id"])
    part = svc.get_part("MTR-DC110")
    assert part["on_hand"] == 5 and part["reserved_qty"] == 0
    assert svc.get_door("D-PSD-0101")["locked"] is False


def test_manager_can_receive_stock(svc):
    part = svc.receive_stock(U_ADMIN, "BELT-1200", 5, note="紧急补货")
    assert part["on_hand"] == 17


def test_locked_door_becomes_reopenable_after_close_and_void(svc):
    first = _open(svc)
    svc.close_work_order(U_ADMIN, first["id"])
    # 关单后同一扇门可以开新单
    second = _open(svc, parts={"BELT-1200": 1})
    assert second["code"].endswith("0002")
    svc.void_work_order(U_ADMIN, second["id"], "重复报修")
    third = _open(svc, parts={"SEN-IR-24": 1})
    assert third["status"] == "open"
    assert svc.verify_consistency()["ok"] is True


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(svc) -> MaintenanceApi:
    return MaintenanceApi(svc)


def _call(api, method, path, body=None, actor=U_STORE, query=None):
    headers = {"X-User-Id": actor} if actor else {}
    return api.handle(method, path, query or {}, body or {}, headers)


def test_api_full_flow(api):
    status, payload = _call(
        api, "POST", "/api/maintain/work-orders",
        {"door_id": "D-PSD-0101", "title": "API 开单", "owner_id": U_REPAIR,
         "parts": [{"sku": "BELT-1200", "qty": 2}]},
        actor=U_REPORT,
    )
    assert status == 200
    wo_id = payload["id"]

    status, payload = _call(
        api, "POST", f"/api/maintain/work-orders/{wo_id}/issue",
        {"items": [{"sku": "BELT-1200", "qty": 2}]},
    )
    assert status == 200

    status, payload = _call(api, "POST", f"/api/maintain/work-orders/{wo_id}/close", {},
                            actor=U_REPAIR)
    assert status == 200 and payload["status"] == "closed"

    status, payload = _call(api, "GET", "/api/maintain/verify")
    assert status == 200 and payload["ok"] is True


def test_api_status_codes(api):
    # 401 匿名
    status, payload = _call(
        api, "POST", "/api/maintain/work-orders", {"door_id": "D-PSD-0101", "title": "x"},
        actor=None,
    )
    assert status == 401

    # 403 权限
    status, payload = _call(
        api, "POST", "/api/maintain/doors", {"door_id": "D9", "name": "九号门"},
        actor=U_REPORT,
    )
    assert status == 403

    # 404
    status, payload = _call(api, "GET", "/api/maintain/parts/NOPE")
    assert status == 404

    # 409 重复开单
    _call(api, "POST", "/api/maintain/work-orders",
          {"door_id": "D-PSD-0101", "title": "第一单"}, actor=U_REPORT)
    status, payload = _call(api, "POST", "/api/maintain/work-orders",
                            {"door_id": "D-PSD-0101", "title": "第二单"}, actor=U_REPORT)
    assert status == 409

    # 400 缺字段
    status, payload = _call(api, "POST", "/api/maintain/receive", {"sku": "BELT-1200"})
    assert status == 400

    # 404 路由
    status, payload = _call(api, "GET", "/api/maintain/nope")
    assert status == 404


def test_http_server_roundtrip(tmp_path, clock):
    """起真实 HTTP 服务，用 urllib 打一遍并发与鉴权。"""

    import urllib.error
    import urllib.request

    from psdline.maintain.server import MaintenanceServer

    service = MaintenanceService(
        Database(str(tmp_path / "http.db")), clock=clock, notifier=RecordingNotifier()
    )
    server = MaintenanceServer(service, port=0)
    base = server.start()
    try:
        def post(path, body, actor):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-User-Id": actor},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())

        status, wo = post("/api/maintain/work-orders",
                          {"door_id": "D-PSD-0101", "title": "http",
                           "parts": [{"sku": "BOLT-M8x30", "qty": 10}]},
                          U_REPORT)
        assert status == 200

        # 匿名 → 401
        req = urllib.request.Request(
            base + "/api/maintain/verify", method="GET",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401

        # 关门权限 → 报修人 403
        req = urllib.request.Request(
            base + f"/api/maintain/work-orders/{wo['id']}/close",
            data=b"{}", headers={"X-User-Id": U_REPORT}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 403
    finally:
        server.stop()
