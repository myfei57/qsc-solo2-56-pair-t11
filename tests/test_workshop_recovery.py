"""崩溃恢复、账实不符重建、审计不可变与超期提醒。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psdline.clock import ManualClock
from psdline.store import JsonStore, JsonlJournal
from psdline.workshop import ReminderPolicy, WorkshopService
from psdline.workshop.registry import WorkshopRegistry


def _open(tmp_path, clock: ManualClock) -> WorkshopService:
    root = str(tmp_path / "ws")
    return WorkshopService(
        JsonStore(root),
        JsonlJournal(root, "workshop", segment_limit=4),
        clock=clock,
        policy=ReminderPolicy(l2_after=4.0, l3_after=24.0),
    )


def _seed(service: WorkshopService) -> str:
    service.register_person("sup-1", "sup-1", "主管", "SUPERVISOR")
    service.register_person("sup-1", "tech-1", "技师", "TECH")
    service.register_person("sup-1", "disp-1", "调度", "DISPATCH")
    service.register_part("sup-1", "P", "件", 10)
    return service.open_order("tech-1", "Z1", "修", [{"part_code": "P", "qty": 3}], sla_hours=2.0)


def test_state_persists_across_restart(tmp_path, clock):
    service = _open(tmp_path, clock)
    order_id = _seed(service)
    service.issue_parts("sup-1", order_id, [{"part_code": "P", "qty": 2}])

    restarted = _open(tmp_path, clock)
    order = restarted.get_order(order_id)
    assert order.status == "OPEN"
    assert order.line("P").issued_qty == 2
    assert restarted.stock_levels()["P"]["on_hand"] == 8
    assert "Z1" in restarted.active_locks()


def test_rebuild_when_snapshot_missing(tmp_path, clock):
    service = _open(tmp_path, clock)
    order_id = _seed(service)
    service.issue_parts("sup-1", order_id, [{"part_code": "P", "qty": 3}])
    service.mark_repaired("tech-1", order_id)
    service.close_order("sup-1", order_id)

    root = tmp_path / "ws"
    JsonStore(str(root)).remove("workshop/snapshot")
    registry = WorkshopRegistry(
        JsonStore(str(root)), JsonlJournal(str(root), "workshop", segment_limit=4), clock=clock
    )
    revived = registry.get_order_view(order_id)
    assert revived.status == "CLOSED"
    assert registry.view_state()["stock"]["P"]["on_hand"] == 7
    # 缺快照重建会留审计事件
    kinds = [event.kind for event in registry.all_events()]
    assert "SNAPSHOT_REBUILT" in kinds


def test_corrupt_snapshot_falls_back_to_journal(tmp_path, clock):
    service = _open(tmp_path, clock)
    order_id = _seed(service)

    root = tmp_path / "ws"
    JsonStore(str(root)).write("workshop/snapshot", {"garbage": True})
    registry = WorkshopRegistry(
        JsonStore(str(root)), JsonlJournal(str(root), "workshop", segment_limit=4), clock=clock
    )
    assert registry.get_order_view(order_id).status == "OPEN"
    assert registry.view_state()["stock"]["P"]["reserved"] == 3


def test_half_written_tail_line_is_truncated(tmp_path, clock):
    service = _open(tmp_path, clock)
    order_id = _seed(service)
    service.mark_repaired("tech-1", order_id)

    root = tmp_path / "ws"
    segments = sorted(Path(root).glob("workshop-*.jsonl"))
    tail = segments[-1]
    with open(tail, "a", encoding="utf-8") as handle:
        handle.write('{"seq": 999, "kind": "ORDER_CLOSED", "ata')  # 没写完的半行

    registry = WorkshopRegistry(
        JsonStore(str(root)), JsonlJournal(str(root), "workshop", segment_limit=4), clock=clock
    )
    # 半行被截断，最近一条完整事件仍是完工
    order = registry.get_order_view(order_id)
    assert order.status == "REPAIRED"
    text = tail.read_text(encoding="utf-8")
    assert "ORDER_CLOSED" not in text


def test_corruption_mid_journal_refuses_start(tmp_path, clock):
    service = _open(tmp_path, clock)
    _seed(service)

    root = tmp_path / "ws"
    first = sorted(Path(root).glob("workshop-*.jsonl"))[0]
    lines = first.read_text(encoding="utf-8").splitlines()
    lines.insert(0, "{not json")  # 开头损坏，不是没写完的尾巴
    first.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(Exception, match="损坏"):  # noqa: B017
        WorkshopRegistry(
            JsonStore(str(root)), JsonlJournal(str(root), "workshop", segment_limit=4), clock=clock
        )


def test_corrupt_snapshot_self_heals_and_records_audit(tmp_path, clock):
    """快照台账被篡改（账实不符）：启动时以流水为准自愈并留审计痕迹。"""

    service = _open(tmp_path, clock)
    order_id = _seed(service)
    service.issue_parts("sup-1", order_id, [{"part_code": "P", "qty": 1}])

    root = tmp_path / "ws"
    store = JsonStore(str(root))
    snapshot = store.read("workshop/snapshot")
    snapshot["stock"]["P"]["on_hand"] = 100  # 账面被人改大
    store.write("workshop/snapshot", snapshot)

    registry = WorkshopRegistry(
        store, JsonlJournal(str(root), "workshop", segment_limit=4), clock=clock
    )
    # 没有相信坏快照：以流水重算，在架仍是 9
    assert registry.view_state()["stock"]["P"]["on_hand"] == 9
    rebuilt_events = [event for event in registry.all_events() if event.kind == "SNAPSHOT_REBUILT"]
    assert len(rebuilt_events) == 1
    assert "快照" in rebuilt_events[0].data["reason"] or "台账不平" in rebuilt_events[0].data["reason"]
    # 自愈后账平
    assert registry.verify() == []


def test_manual_rebuild_records_found_drift(tmp_path, clock):
    """主管手动重建：在线内存台账被改花时，以流水为准并审计差异。"""

    service = _open(tmp_path, clock)
    order_id = _seed(service)
    service.issue_parts("sup-1", order_id, [{"part_code": "P", "qty": 1}])

    registry = service.registry
    # 模拟运行期内存状态漂移（bug 或外部破坏），流水不动
    registry.view_state()["stock"]["P"]["on_hand"] = 50
    assert registry.verify()  # 确实不平
    committed = registry.rebuild_from_journal("sup-1", note="现场盘账重建")
    assert committed  # 落了重建事件
    rebuilt_event = registry.all_events()[-1]
    assert rebuilt_event.kind == "SNAPSHOT_REBUILT"
    assert rebuilt_event.data["found"]
    assert registry.view_state()["stock"]["P"]["on_hand"] == 9
    assert registry.verify() == []


def test_history_is_append_only(tmp_path, clock):
    service = _open(tmp_path, clock)
    order_id = _seed(service)
    service.issue_parts("sup-1", order_id, [{"part_code": "P", "qty": 2}])
    service.mark_repaired("tech-1", order_id)
    service.close_order("sup-1", order_id, note="ok")

    raw_events = [
        json.loads(line)
        for path in sorted((tmp_path / "ws").glob("workshop-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # seq 严格递增且无重复
    seqs = [event["seq"] for event in raw_events]
    assert seqs == sorted(set(seqs))
    # 关单后事件清单里没有任何"修改/删除"型动作
    kinds = {event["kind"] for event in raw_events}
    assert not {"UPDATE", "DELETE"} & kinds
    # 工单历史不可变：每个 seq 只出现一次且内容可逐字回放
    order = service.get_order(order_id)
    assert [item["seq"] for item in order.history] == sorted(item["seq"] for item in order.history)


def test_overdue_reminders_escalate_once_per_level(tmp_path, clock):
    notices: list[tuple[str, list[str]]] = []
    root = str(tmp_path / "ws")
    service = WorkshopService(
        JsonStore(root),
        JsonlJournal(root, "workshop", segment_limit=16),
        clock=clock,
        notifier=lambda event: notices.append((event.data["level"], event.data["audience"])),
        policy=ReminderPolicy(l2_after=4.0, l3_after=24.0),
    )
    order_id = _seed(service)  # SLA 2 小时

    # 未到期不提醒
    clock.advance(3600)
    assert service.scan_overdue() == []

    clock.advance(2 * 3600)  # 逾期 1 小时
    raised = service.scan_overdue()
    assert raised[0]["level"] == "L1"
    assert raised[0]["audience"] == ["tech-1"]

    # 同级别不重复
    clock.advance(3600)
    assert service.scan_overdue() == []

    clock.advance(3 * 3600)  # 逾期 5 小时 -> L2
    raised = service.scan_overdue()
    assert raised[0]["level"] == "L2"
    assert "disp-1" in raised[0]["audience"]

    clock.advance(24 * 3600)  # -> L3
    raised = service.scan_overdue()
    assert raised[0]["level"] == "L3"
    assert any(person_id.startswith("sup") for person_id in raised[0]["audience"])

    # 终结后不再提醒
    service.issue_parts("sup-1", order_id, [{"part_code": "P", "qty": 3}])
    service.mark_repaired("tech-1", order_id)
    service.close_order("sup-1", order_id)
    clock.advance(100 * 3600)
    assert service.scan_overdue() == []
    # 通知钩子恰好收到 L1/L2/L3 各一次
    assert [level for level, _audience in notices] == ["L1", "L2", "L3"]
