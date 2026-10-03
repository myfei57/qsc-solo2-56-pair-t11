"""车间域端到端演示（临时目录，不污染现场数据）。

演示线索（用可推进的 ManualClock 演完整生命周期，包括跨班与超期升级）：

1. 主管建档：白班/夜班技师、仓管、调度、备件目录；
2. 白班技师报修 A 区滑动门，开单即锁门、预占电机与密封圈；
3. 并发抢同一批件：另一张工单预占失败，库存一分不动；
4. 夜班仓管跨班发料，整批中一件超额 → 整批回滚；改单后发料成功；
5. 时钟推过 SLA：L1 提醒负责人；再推到 L3：升级主管；
6. 技师完工，主管关单，未领预占自动回补、门区解锁；
7. 另一张单走废单路径：先演示"已领未清不许废"，退 1 件 + 主管核销 1 件后废单成功；
8. 盘点盘亏一件，主管批准调账；
9. 删除快照后重启：从只追加流水完整重建，账实一致；工单历史原样可读。

运行：``PYTHONPATH=. python -m psdline.workshop.demo``
"""

from __future__ import annotations

import tempfile

from ..clock import ManualClock
from ..store import JsonStore, JsonlJournal
from .errors import InventoryShortage, InvalidTransition, PermissionDenied
from .registry import WorkshopRegistry
from .reminders import ReminderPolicy
from .service import WorkshopService


def _hr(title: str) -> None:
    print()
    print("=" * 8 + f" {title} " + "=" * 8)


def run() -> None:
    clock = ManualClock()
    policy = ReminderPolicy(l2_after=4.0, l3_after=24.0)
    notices: list[str] = []

    with tempfile.TemporaryDirectory(prefix="workshop-demo-") as root:
        service = WorkshopService(
            JsonStore(root),
            JsonlJournal(root, "workshop", segment_limit=64),
            clock=clock,
            notifier=lambda event: notices.append(
                f"[外部通知] {event.data['level']} -> {event.data['audience']} 工单 {event.data['order_id']}"
            ),
            policy=policy,
        )

        _hr("1. 建档")
        service.register_person("sup-1", "sup-1", "王主管", "SUPERVISOR")
        service.register_person("sup-1", "tech-day", "李白班", "TECH", shift="DAY")
        service.register_person("sup-1", "tech-night", "赵夜班", "TECH", shift="NIGHT")
        service.register_person("sup-1", "store-1", "孙仓管", "STORE")
        service.register_person("sup-1", "disp-1", "周调度", "DISPATCH")
        service.register_part("store-1", "MOTOR-01", "门驱电机", 2, unit="台", location="A-01")
        service.register_part("store-1", "SEAL-08", "防水密封圈", 10, unit="个", location="B-03")
        print("人员:", [p.name + "/" + p.shift for p in service.list_people()])
        print("备件目录:", [part.code for part in service.list_parts()])
        print("库存:", service.stock_levels())

        _hr("2. 白班开单：A 区门故障，锁门 + 预占")
        order_id = service.open_order(
            "tech-day",
            "ZONE-A",
            "1 号滑动门无法关闭",
            [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 4}],
            owner_id="tech-day",
            sla_hours=2.0,
        )
        print("工单:", order_id, "| 门区锁:", list(service.active_locks()))
        print("预占后:", service.stock_levels())

        _hr("3. 并发抢件 + 同门重复开单，都应被拒")
        try:
            service.open_order(
                "tech-day",
                "ZONE-B",
                "也想换电机",
                [{"part_code": "MOTOR-01", "qty": 2}],
            )
        except InventoryShortage as error:
            print("抢 2 台电机被拒（可分配只剩 1 台）:", error.message)
        try:
            service.open_order("tech-day", "ZONE-A", "同一门再开一单", [])
        except InvalidTransition as error:
            print("同门锁冲突被拒:", error.message)
        print("库存未变:", service.stock_levels()["MOTOR-01"])

        _hr("4. 跨班领料：白班开的单，夜班仓管发；整批失败回滚")
        clock.advance(2.5 * 3600)  # 刚过 SLA 半小时，先触发 L1
        print("扫描（L1 只通知负责人）:", service.scan_overdue())
        clock.advance(9 * 3600)  # 入夜
        try:
            service.issue_parts(
                "store-1",
                order_id,
                [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 9}],
                handoff="白班->夜班",
            )
        except InventoryShortage as error:
            print("密封圈超额（预占 4，领 9）整批被拒:", error.message)
        print("回滚后在架不变（电机仍是 2 台）:", service.stock_levels()["MOTOR-01"])
        service.issue_parts(
            "store-1",
            order_id,
            [{"part_code": "MOTOR-01", "qty": 1}, {"part_code": "SEAL-08", "qty": 3}],
            handoff="白班->夜班",
        )
        print("发料成功:", service.stock_levels())

        _hr("5. 超期提醒分级升级")
        print("扫描（入夜后自动升 L2，L1 已发过不重复）:", service.scan_overdue())
        clock.advance(24 * 3600)
        print("扫描（跨 L2 直升 L3，升级主管）:", service.scan_overdue())
        for notice in notices:
            print(" ", notice)

        _hr("6. 完工 -> 主管关单（未领密封圈 1 个自动回补，门解锁）")
        service.mark_repaired("tech-night", order_id, note="夜间更换电机与 3 个密封圈")
        try:
            service.close_order("tech-night", order_id)
        except PermissionDenied as error:
            print("技师不能关单:", error.message)
        service.close_order("sup-1", order_id, note="验收通过")
        order = service.get_order(order_id)
        print("状态:", order.status, "| 历史条数:", len(order.history), "| 门区锁:", service.active_locks())
        print("回补后库存:", service.stock_levels())

        _hr("7. 废单路径：已领未清必须先退料/核销")
        second = service.open_order(
            "tech-day",
            "ZONE-C",
            "2 号门异响待观察",
            [{"part_code": "SEAL-08", "qty": 2}],
            owner_id="tech-day",
        )
        service.issue_parts("store-1", second, [{"part_code": "SEAL-08", "qty": 2}])
        try:
            service.void_order("sup-1", second, "观察后不需修")
        except InvalidTransition as error:
            print("废单被拒:", error.message)
        service.return_parts("store-1", second, [{"part_code": "SEAL-08", "qty": 1}], reason="未拆封退回")
        service.write_off_parts("sup-1", second, [{"part_code": "SEAL-08", "qty": 1}], reason="已试装到 3 号门")
        service.void_order("sup-1", second, "观察后不需修")
        print("退 1 件 + 主管核销 1 件后废单成功，状态:", service.get_order(second).status, "| 锁:", list(service.active_locks()))

        _hr("8. 盘点：密封圈实物少 1 个（账 6 实 5），仓管上报、主管批准调账")
        try:
            service.recount("store-1", {"SEAL-08": 5}, note="货架少一件", require_approval=True)
        except PermissionDenied as error:
            print("仓管不能直接调账:", error.message)
        result = service.recount("sup-1", {"SEAL-08": 5}, note="确认损坏丢弃")
        print("调整:", result["adjusted"])

        _hr("9. 删快照重启：从流水重建")
        problems = service.verify_stock()
        print("删除前对账:", "账平" if not problems else problems)
        print("主管手动重建入口（无差异时不落事件）:", service.recover_stock("sup-1", note="例行"))
        fresh_store = JsonStore(root)
        fresh_store.remove("workshop/snapshot")
        rebuilt = WorkshopRegistry(
            fresh_store,
            JsonlJournal(root, "workshop", segment_limit=64),
            clock=clock,
        )
        revived = rebuilt.get_order_view(order_id)
        print("缺快照启动后自动重建，工单状态:", revived.status, "| 历史不可变，条数:", len(revived.history))
        print("重建后台账:", rebuilt.view_state()["stock"]["SEAL-08"])
        print("ZONE-A 锁动作轨迹:", len(service.lock_history("ZONE-A")), "条（LOCK + UNLOCK）")
        print("流水事件总数:", len(rebuilt.all_events()))
        print("全部工单:", [(item.id[:12], item.status) for item in service.list_orders()])


if __name__ == "__main__":
    run()
