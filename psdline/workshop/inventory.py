"""库存投影与台账恒等式。

备件账分三层数量：

on_hand     货架上的实物库存（领料出库才减少，开单预占不动它）
reserved    被活动工单预占、别人不能再动的量
available   可分配量 = on_hand - reserved（开单预占用它做校验）

事件到账的对应关系：

PART_REGISTERED      on_hand/registered_total += qty
ORDER_OPENED         reserved += 每行预占量
PARTS_ISSUED         on_hand -= qty、reserved -= qty、issued_total += qty
PARTS_RETURNED       on_hand += qty、returned_total += qty（已领件实物退回）
ORDER_CLOSED/VOIDED  reserved -= 事件自带的 released 清单（未领量自动回补）
STOCK_RECOUNTED      on_hand += delta、adjust_total += delta（盘盈盘亏，带符号）
PARTS_WRITTEN_OFF    物不动，只记 written_off_total（件已出库，核销是责任记账）

台账恒等式（:func:`check_invariants` 每次提交和重建后都校验）：

1. on_hand == registered_total - issued_total + returned_total + adjust_total
2. 0 <= reserved <= on_hand
3. 所有累计列非负

违反任意一条即 :class:`~psdline.workshop.errors.StockDrift`，拒绝提交或触发
快照重建对账。
"""

from __future__ import annotations

from .events import WorkshopEvent
from .errors import InventoryShortage, StockDrift

#: 每个备件一进入台账就有的七列账。
LEDGER_COLUMNS = (
    "on_hand",
    "reserved",
    "registered_total",
    "issued_total",
    "returned_total",
    "written_off_total",
    "adjust_total",
)


def new_stock_state() -> dict:
    """空台账（dict，便于 JSON 快照）。"""

    return {}


def _entry(state: dict, part_code: str) -> dict:
    return state.setdefault(
        part_code,
        {column: 0 for column in LEDGER_COLUMNS},
    )


def available(state: dict, part_code: str) -> int:
    """可分配量；未登记备件一律为 0。"""

    entry = state.get(part_code)
    if entry is None:
        return 0
    return entry["on_hand"] - entry["reserved"]


def on_hand(state: dict, part_code: str) -> int:
    entry = state.get(part_code)
    return entry["on_hand"] if entry else 0


def reserved(state: dict, part_code: str) -> int:
    entry = state.get(part_code)
    return entry["reserved"] if entry else 0


def assert_can_reserve(state: dict, lines: list[dict]) -> None:
    """校验一批预占是否可行。

    同一工单可能一次预占多种件，必须按"申请累计"一起看：先算每个件的总申请
    量再比对可分配量，避免工单内重复行让校验漏过。库存不足直接抛
    :class:`InventoryShortage`，由注册表整批回滚。
    """

    wanted: dict[str, int] = {}
    for line in lines:
        code = str(line["part_code"])
        qty = int(line["qty"])
        if qty <= 0:
            raise InventoryShortage(f"{code} 预占数量必须为正数，收到 {qty}")
        wanted[code] = wanted.get(code, 0) + qty
    missing = []
    for code, qty in wanted.items():
        free = available(state, code)
        if qty > free:
            missing.append(f"{code}: 需 {qty}，可分配 {free}")
    if missing:
        raise InventoryShortage("备件可分配量不足: " + "；".join(missing))


def assert_can_issue(state: dict, moves: list[dict]) -> None:
    """校验一批领料：只能领出该单已预占、且实物在架的量。"""

    wanted: dict[str, int] = {}
    for move in moves:
        code = str(move["part_code"])
        qty = int(move["qty"])
        if qty <= 0:
            raise InventoryShortage(f"{code} 领料数量必须为正数，收到 {qty}")
        wanted[code] = wanted.get(code, 0) + qty
    problems = []
    for code, qty in wanted.items():
        entry = state.get(code)
        if entry is None:
            problems.append(f"{code}: 未登记的备件")
            continue
        if qty > entry["reserved"]:
            problems.append(f"{code}: 要领 {qty}，该单预占余量不足（总预占余量 {entry['reserved']}）")
        if qty > entry["on_hand"]:
            problems.append(f"{code}: 要领 {qty}，实物在架仅 {entry['on_hand']}")
    if problems:
        raise InventoryShortage("领料被拒: " + "；".join(problems))


def assert_can_adjust(state: dict, adjustments: list[dict]) -> None:
    """校验盘点调整：调整后不允许出现负库存或预占超过在架。"""

    totals: dict[str, int] = {}
    for move in adjustments:
        totals[str(move["part_code"])] = totals.get(str(move["part_code"]), 0) + int(move["delta"])
    problems = []
    for code, delta in totals.items():
        entry = state.get(code)
        if entry is None:
            problems.append(f"{code}: 未登记的备件")
            continue
        new_on_hand = entry["on_hand"] + delta
        if new_on_hand < 0:
            problems.append(f"{code}: 调整后实物为负（{new_on_hand}）")
        if new_on_hand < entry["reserved"]:
            problems.append(f"{code}: 调整后在架 {new_on_hand} 低于已预占 {entry['reserved']}")
    if problems:
        raise StockDrift("盘点调整被拒: " + "；".join(problems))


def apply(state: dict, event: WorkshopEvent) -> None:
    """把一条事件投影进台账。只做算术，不做权限/状态校验。"""

    kind = event.kind
    data = event.data
    if kind == "PART_REGISTERED":
        entry = _entry(state, str(data["part_code"]))
        qty = int(data["qty"])
        entry["on_hand"] += qty
        entry["registered_total"] += qty
    elif kind == "ORDER_OPENED":
        for line in data.get("lines", []):
            entry = _entry(state, str(line["part_code"]))
            entry["reserved"] += int(line["qty"])
    elif kind == "PARTS_ISSUED":
        for move in data.get("moves", []):
            entry = _entry(state, str(move["part_code"]))
            qty = int(move["qty"])
            entry["on_hand"] -= qty
            entry["reserved"] -= qty
            entry["issued_total"] += qty
    elif kind == "PARTS_RETURNED":
        for move in data.get("moves", []):
            entry = _entry(state, str(move["part_code"]))
            qty = int(move["qty"])
            entry["on_hand"] += qty
            entry["returned_total"] += qty
    elif kind == "PARTS_WRITTEN_OFF":
        for move in data.get("moves", []):
            entry = _entry(state, str(move["part_code"]))
            entry["written_off_total"] += int(move["qty"])
    elif kind in ("ORDER_CLOSED", "ORDER_VOIDED"):
        for move in data.get("released", []):
            entry = _entry(state, str(move["part_code"]))
            entry["reserved"] -= int(move["qty"])
    elif kind == "STOCK_RECOUNTED":
        for move in data.get("adjustments", []):
            entry = _entry(state, str(move["part_code"]))
            delta = int(move["delta"])
            entry["on_hand"] += delta
            entry["adjust_total"] += delta


def check_invariants(state: dict) -> list[str]:
    """返回台账违例描述列表；空列表代表账平。"""

    problems: list[str] = []
    # adjust_total 是带符号累计（盘盈正、盘亏负），不参与非负校验。
    non_negative = tuple(column for column in LEDGER_COLUMNS if column != "adjust_total")
    for code, entry in sorted(state.items()):
        for column in non_negative:
            if entry[column] < 0:
                problems.append(f"{code}: {column} 出现负值 {entry[column]}")
        rebuilt_on_hand = (
            entry["registered_total"]
            - entry["issued_total"]
            + entry["returned_total"]
            + entry["adjust_total"]
        )
        if rebuilt_on_hand != entry["on_hand"]:
            problems.append(
                f"{code}: 流水重算在架 {rebuilt_on_hand} 与台账在架 {entry['on_hand']} 不符"
            )
        if entry["reserved"] > entry["on_hand"]:
            problems.append(
                f"{code}: 预占 {entry['reserved']} 超过在架 {entry['on_hand']}"
            )
    return problems


def assert_consistent(state: dict) -> None:
    problems = check_invariants(state)
    if problems:
        raise StockDrift("库存台账不平: " + "；".join(problems))


def rebuild(events: list[WorkshopEvent]) -> dict:
    """从空账开始重放全部事件，得到重建台账（恢复/对账用）。"""

    state = new_stock_state()
    for event in events:
        apply(state, event)
    assert_consistent(state)
    return state
