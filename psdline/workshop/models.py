"""车间域只读视图模型。

这些数据类是事件投影的对外读数（``as_dict`` 便于控制台/演示序列化）。规则
判定不放在这里，放在 ``orders`` / ``inventory`` / ``service`` 模块，保证模型
本身没有可变行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 技师：开单、报修、参与领料交接。
ROLE_TECH = "TECH"
#: 仓管：实物发料、退料入库。
ROLE_STORE = "STORE"
#: 主管：关单、废单、核销、盘亏调整的唯一批准人。
ROLE_SUPERVISOR = "SUPERVISOR"
#: 调度：开单派活，跨班交接的默认知会人。
ROLE_DISPATCH = "DISPATCH"

ROLES = frozenset({ROLE_TECH, ROLE_STORE, ROLE_SUPERVISOR, ROLE_DISPATCH})

#: 工单活动态：可领料、可报修、可超期。
ORDER_OPEN = "OPEN"
#: 已报修完工，等待主管关单验收；仍可补领料。
ORDER_REPAIRED = "REPAIRED"
#: 已关单（正常终结），历史冻结、门区已解锁。
ORDER_CLOSED = "CLOSED"
#: 已废单（异常终结），历史冻结、门区已解锁。
ORDER_VOIDED = "VOIDED"

#: 终结态集合。
TERMINAL_STATUSES = frozenset({ORDER_CLOSED, ORDER_VOIDED})
#: 仍允许领料的状态（报修完工后补领一个密封圈是现场常态）。
ISSUABLE_STATUSES = frozenset({ORDER_OPEN, ORDER_REPAIRED})


@dataclass(frozen=True)
class Person:
    """登记在册的人员。"""

    id: str
    name: str
    role: str
    shift: str = "DAY"

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role, "shift": self.shift}

    @classmethod
    def from_dict(cls, payload: dict) -> "Person":
        return cls(
            id=str(payload["id"]),
            name=str(payload["name"]),
            role=str(payload["role"]),
            shift=str(payload.get("shift", "DAY")),
        )


@dataclass(frozen=True)
class Part:
    """备件目录条目（数量在库存投影里，不在这里）。"""

    code: str
    name: str
    unit: str = "个"
    location: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "unit": self.unit, "location": self.location}

    @classmethod
    def from_dict(cls, payload: dict) -> "Part":
        return cls(
            code=str(payload["code"]),
            name=str(payload["name"]),
            unit=str(payload.get("unit", "个")),
            location=str(payload.get("location", "")),
        )


@dataclass(frozen=True)
class OrderLine:
    """工单上的一行备件需求与履约数量。

    数量关系（投影恒等式，测试会逐条校验）：

    * ``issued_qty <= reserved_qty``——只能领走预占过的量；
    * ``returned_qty + written_off_qty <= issued_qty``——已领件只能退回或核销；
    * ``unissued_qty = reserved_qty - issued_qty``——关单/废单时自动释放的量。
    """

    part_code: str
    reserved_qty: int
    issued_qty: int = 0
    returned_qty: int = 0
    written_off_qty: int = 0

    @property
    def unissued_qty(self) -> int:
        """未领的预占量，终结时自动回补。"""

        return self.reserved_qty - self.issued_qty

    @property
    def outstanding_issued_qty(self) -> int:
        """已领但尚未退库/核销的量；废单前必须清零。"""

        return self.issued_qty - self.returned_qty - self.written_off_qty

    def as_dict(self) -> dict:
        return {
            "part_code": self.part_code,
            "reserved_qty": self.reserved_qty,
            "issued_qty": self.issued_qty,
            "returned_qty": self.returned_qty,
            "written_off_qty": self.written_off_qty,
            "unissued_qty": self.unissued_qty,
            "outstanding_issued_qty": self.outstanding_issued_qty,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "OrderLine":
        return cls(
            part_code=str(payload["part_code"]),
            reserved_qty=int(payload["reserved_qty"]),
            issued_qty=int(payload.get("issued_qty", 0)),
            returned_qty=int(payload.get("returned_qty", 0)),
            written_off_qty=int(payload.get("written_off_qty", 0)),
        )


@dataclass(frozen=True)
class WorkOrder:
    """工单视图。

    ``status`` 由事件流严格投影：开单为 ``OPEN``，报修完工后为 ``REPAIRED``，
    关单/废单进入终结态。``lines`` 是备件履约明细，``history`` 是不可变事件
    摘要列表。
    """

    id: str
    door_zone: str
    title: str
    status: str
    opened_by: str
    owner_id: str
    opened_at: float
    due_at: float
    repaired_by: str = ""
    repaired_at: float = 0.0
    closed_by: str = ""
    closed_at: float = 0.0
    voided_by: str = ""
    voided_at: float = 0.0
    void_reason: str = ""
    close_note: str = ""
    lines: tuple[OrderLine, ...] = field(default_factory=tuple)
    history: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def line(self, part_code: str) -> OrderLine | None:
        for item in self.lines:
            if item.part_code == part_code:
                return item
        return None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "door_zone": self.door_zone,
            "title": self.title,
            "status": self.status,
            "opened_by": self.opened_by,
            "owner_id": self.owner_id,
            "opened_at": self.opened_at,
            "due_at": self.due_at,
            "repaired_by": self.repaired_by,
            "repaired_at": self.repaired_at,
            "closed_by": self.closed_by,
            "closed_at": self.closed_at,
            "voided_by": self.voided_by,
            "voided_at": self.voided_at,
            "void_reason": self.void_reason,
            "close_note": self.close_note,
            "lines": [item.as_dict() for item in self.lines],
            "history": list(self.history),
        }
