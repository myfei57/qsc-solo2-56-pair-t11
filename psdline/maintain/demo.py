"""工单备件全流程演示（不依赖真实挂钟，用可推进时钟演完整生命周期）。

覆盖：开单预占 → 跨班改派 → 并发安全领料 → 权限拦截 → 完工不解锁 → 超期两级
提醒 → 关单解锁；以及废单后已领件必须人工退料、盘点双人控制、账实漂移核对与
投影恢复。给运维做交付前自检用：``python -m psdline.maintain demo``。
"""

from __future__ import annotations

import concurrent.futures
import json
import tempfile

from ..clock import ManualClock
from .db import Database
from .errors import ConflictError, PermissionDenied
from .notify import RecordingNotifier
from .service import MaintenanceService


def _print(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def run_demo(db_path: str | None = None) -> dict:
    """跑一遍完整业务流程，返回关键结果字典（供测试断言）。"""

    clock = ManualClock()
    notifier = RecordingNotifier()
    path = db_path or tempfile.mktemp(prefix="maintain-demo-", suffix=".db")
    service = MaintenanceService(
        Database(path), clock=clock, notifier=notifier,
        sla_hours=24.0, escalate_after_hours=24.0,
    )

    # 1) 报修开单：门 D-PSD-0101，预占电机 1 台、皮带 2 条 ---------------------
    wo = service.open_work_order(
        "u-report", "D-PSD-0101", "1 号门开关不到位，异响",
        {"MTR-DC110": 1, "BELT-1200": 2},
        owner_id="u-repair", description="早高峰后巡检发现",
        shift="早班",
    )
    wo_id = wo["id"]
    _print("开单（预占备件、门上锁）", {
        "code": wo["code"], "status": wo["status"],
        "door": service.get_door("D-PSD-0101"),
        "motor": service.get_part("MTR-DC110"),
    })

    # 2) 同一扇门再开单必须被拒（部分唯一索引） --------------------------------
    blocked = False
    try:
        service.open_work_order("u-report", "D-PSD-0101", "重复报修", {"BELT-1200": 1})
    except ConflictError:
        blocked = True

    # 3) 跨班改派：管理员把负责人从李维修改给晚班维修账号（演示里新建一个） ----
    service.create_user("u-admin", "u-repair2", "孙维修(晚班)", "repair")
    service.reassign("u-admin", wo_id, "u-repair2", shift="晚班", note="早班交班")

    # 4) 并发领料：两笔不同工单的合法发料并发提交，写事务串行化，账不能错 ----
    sensor_wo = service.open_work_order(
        "u-report", "D-PSD-0201", "传感器更换", {"SEN-IR-24": 8}, owner_id="u-repair",
    )
    motor_wo = service.open_work_order(
        "u-report", "D-PSD-0102", "2 号门电机故障", {"MTR-DC110": 4}, owner_id="u-repair",
    )

    def issue(target_wo: str, items: dict) -> str:
        try:
            service.issue_parts("u-store", target_wo, items)
            return "ok"
        except ConflictError as error:
            return f"rejected: {error.message}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(issue, sensor_wo["id"], {"SEN-IR-24": 8}),
            pool.submit(issue, motor_wo["id"], {"MTR-DC110": 4}),
        ]
        race_results = [f.result() for f in futures]

    # 4b) 多行领料部分失败必须整笔回滚：皮带合法、电机超领 → 皮带也不能被扣 ----
    belt_before = service.get_part("BELT-1200")["on_hand"]
    partial_rollback = False
    try:
        service.issue_parts("u-store", wo_id, {"BELT-1200": 1, "MTR-DC110": 4})
    except ConflictError:
        partial_rollback = True
    belt_after = service.get_part("BELT-1200")["on_hand"]
    partial_rollback_ok = partial_rollback and belt_before == belt_after

    # 5) 权限：报修人不能发料、不能关单 -----------------------------------------
    denied_issue = False
    try:
        service.issue_parts("u-report", wo_id, {"BELT-1200": 1})
    except PermissionDenied:
        denied_issue = True

    # 6) 正常发料 + 完工：完工后门仍然锁着 --------------------------------------
    issued = service.issue_parts("u-store", wo_id, {"MTR-DC110": 1, "BELT-1200": 2})
    service.start_work("u-repair2", wo_id)
    service.complete_work("u-repair2", wo_id, "更换电机与皮带，开关 50 次无异常")
    door_after_complete = service.get_door("D-PSD-0101")

    # 7) 完工长期不验收 → done_unclosed 两级提醒 --------------------------------
    clock.advance(25 * 3600)
    first_scan = service.scan_overdue()
    clock.advance(24 * 3600)
    second_scan = service.scan_overdue()
    # 重复扫描不重复通知
    third_scan = service.scan_overdue()

    # 8) 负责人关单，门解锁 ------------------------------------------------------
    service.close_work_order("u-repair2", wo_id)
    door_after_close = service.get_door("D-PSD-0101")

    # 9) 废单路径：并发阶段已从 motor_wo 领走 4 台电机，管理员废单 → 必须人工退料 -
    before_void = service.get_part("MTR-DC110")
    service.void_work_order("u-admin", motor_wo["id"], "复查为误报，门本体正常")
    after_void = service.get_part("MTR-DC110")  # 实物不自动回补
    tasks = service.list_return_tasks()
    task = tasks[0]
    # 找回 3 台先退，剩 1 台待查
    service.confirm_return("u-store", task["id"], 3, note="完好件退回")
    partial_task = service.get_return_task(task["id"])
    motor_after_partial = service.get_part("MTR-DC110")
    service.confirm_return("u-store", task["id"], 1, note="损坏件，按报废处理")
    motor_after_full = service.get_part("MTR-DC110")

    # 10) 盘点双人控制：库管发起、管理员批准才调账 ------------------------------
    take = service.create_stocktake("u-store", "BELT-1200", 9, note="货架实点 9 条")
    # 库管自己不能批
    denied_approve = False
    try:
        service.decide_stocktake("u-store", take["id"], True)
    except PermissionDenied:
        denied_approve = True
    approved = service.decide_stocktake("u-admin", take["id"], True, note="差异系领用漏登")

    # 11) 一致性核对全程必须干净（除人为制造漂移的演示外） ----------------------
    healthy = service.verify_consistency()

    # 12) 不可变历史：直接改事件表会被触发器拒绝 --------------------------------
    history_tampered = False
    first_event_id = service.work_order_history(wo_id)[0]["id"]
    try:
        service.db.connect().execute(
            "UPDATE wo_events SET kind = 'hacked' WHERE id = ?", (first_event_id,)
        )
    except Exception:
        history_tampered = True

    history = service.work_order_history(wo_id)

    result = {
        "wo_code": wo["code"],
        "duplicate_open_blocked": blocked,
        "concurrent_issue": race_results,
        "partial_rollback_ok": partial_rollback_ok,
        "stock_after_concurrent_issue": {
            "SEN-IR-24": service.get_part("SEN-IR-24")["on_hand"],
            "MTR-DC110": service.get_part("MTR-DC110")["on_hand"],
        },
        "denied_issue_for_reporter": denied_issue,
        "issued_lines": issued["parts"],
        "door_locked_after_complete": door_after_complete["locked"],
        "reminders_level1": first_scan,
        "reminders_level2": second_scan,
        "reminders_rescan_dedup": third_scan,
        "door_unlocked_after_close": not door_after_close["locked"],
        "stock_before_void": before_void["on_hand"],
        "stock_after_void_no_auto_restock": after_void["on_hand"],
        "return_task_partial": partial_task,
        "stock_after_partial_return": motor_after_partial["on_hand"],
        "stock_after_full_return": motor_after_full["on_hand"],
        "stocktake_self_approve_denied": denied_approve,
        "stocktake_approved": approved,
        "verify_healthy": healthy,
        "history_tamper_rejected": history_tampered,
        "history_events": [event["kind"] for event in history],
        "notifications": [item.as_dict() for item in notifier.sent],
    }
    _print("演示结果", result)
    return result
