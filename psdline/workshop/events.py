"""车间事件定义。

车间域采用事件溯源：状态不是直接改出来的，而是由一串只追加事件投影出来的。
事件一旦写入 :class:`~psdline.store.JsonlJournal` 就不可修改、不可删除（与控制
服务的审计流水同样的只追加 JSONL 保证），这就是"工单历史不可变"的落点。

事件清单与库存账的关系（库存数量恒等式
``on_hand = issued_total + available + reserved``，见 ``inventory`` 模块）：

PERSON_REGISTERED      登记人员（角色决定权限）
PART_REGISTERED        登记备件并给出初始在库
ORDER_OPENED           开单，门区上锁，逐行预占（reserved 增加）
PARTS_ISSUED           领料出库（on_hand 减少、reserved 转已出库）
PARTS_RETURNED         退库（未领的退预占；已领的退实物）
PARTS_WRITTEN_OFF      主管核销已领件，出库不回补（需原因）
ORDER_REPAIRED         技师报修完工，等待主管关单
ORDER_CLOSED           主管关单：确认消耗、释放未领预占、门区解锁
ORDER_VOIDED           主管废单：自动回补未领预占、门区解锁
DOOR_LOCKED            门区物理锁定登记（随开单同批提交）
DOOR_UNLOCKED          门区物理解锁登记（随关单/废单同批提交）
REMINDER_RAISED        超期提醒（含级别）
STOCK_RECOUNTED        盘点差异调整（需主管批准的调整量与原因）
SNAPSHOT_REBUILT       快照台账从流水重建（审计标记，记录发现的偏差）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ids import IdFactory

_EVENT_IDS = IdFactory("evt")

#: 全部允许进入流水的事件类型。注册表只接受这份清单内的事件。
EVENT_KINDS = frozenset(
    {
        "PERSON_REGISTERED",
        "PART_REGISTERED",
        "ORDER_OPENED",
        "PARTS_ISSUED",
        "PARTS_RETURNED",
        "PARTS_WRITTEN_OFF",
        "ORDER_REPAIRED",
        "ORDER_CLOSED",
        "ORDER_VOIDED",
        "DOOR_LOCKED",
        "DOOR_UNLOCKED",
        "REMINDER_RAISED",
        "STOCK_RECOUNTED",
        "SNAPSHOT_REBUILT",
    }
)

#: 库存移动方向：正数代表进实物库存，负数代表出实物库存。
MOVEMENT_KINDS = frozenset(
    {
        "PART_REGISTERED",
        "PARTS_ISSUED",
        "PARTS_RETURNED",
        "PARTS_WRITTEN_OFF",
        "STOCK_RECOUNTED",
    }
)

#: 工单终结事件，出现后工单历史冻结。
TERMINAL_EVENTS = frozenset({"ORDER_CLOSED", "ORDER_VOIDED"})


@dataclass(frozen=True)
class WorkshopEvent:
    """一条不可变车间事件。

    ``seq`` 是提交时由注册表分配的流水序号（全车间严格递增）；``at`` 为提交
    时刻 Unix 时间戳；``actor`` 是操作人工号；``data`` 携带事件细节。
    """

    id: str
    seq: int
    kind: str
    at: float
    actor: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "actor": self.actor,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WorkshopEvent":
        return cls(
            id=str(payload.get("id", "")),
            seq=int(payload.get("seq", 0)),
            kind=str(payload.get("kind", "")),
            at=float(payload.get("at", 0.0)),
            actor=str(payload.get("actor", "")),
            data=dict(payload.get("data", {})),
        )


def make_event(kind: str, at: float, actor: str, data: dict) -> WorkshopEvent:
    """构造一条事件（序号由注册表提交时补填，这里先给 0）。"""

    if kind not in EVENT_KINDS:
        raise ValueError(f"未知事件类型: {kind}")
    return WorkshopEvent(
        id=_EVENT_IDS.next(),
        seq=0,
        kind=kind,
        at=at,
        actor=actor,
        data=dict(data),
    )
