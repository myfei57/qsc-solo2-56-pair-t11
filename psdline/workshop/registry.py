"""事件注册表：提交协议、快照、回放与崩溃恢复。

一次业务动作（开单、领料、关单……）由服务层翻译成一批候选事件，注册表负责
把这批事件做成一个不可分割的事务。提交协议：

1. 取全局大锁（进程内并发与跨班并发的串行化点）；
2. 在内存状态的**深拷贝**上试投影整批事件，服务层传入的校验闭包也在锁内、
   针对同一状态执行；任一断言抛异常 → 放弃拷贝，现场状态与流水都不变
   （"部分失败回滚"）；
3. 逐个分配递增 seq，追加到只追加 JSONL 流水并 fsync；
4. 用试投影成功的新状态替换内存状态；
5. 原子写快照（临时文件 + os.replace，复用 JsonStore）。

崩溃恢复：启动时读快照拿到基线，再把流水里 seq 大于快照 last_seq 的事件补齐；
若快照丢失或账不平（账实不符），直接从流水第 1 条全量重放重建，并落一条
SNAPSHOT_REBUILT 审计事件。

并发正确性不依赖调用方自觉：所有改状态的路径都经过这把锁。跨进程部署时
流水目录应位于支持原子 rename 的共享卷；当前仓库其余模块同为单进程多线程
模型，这里与之一致。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Callable, Iterable

from ..clock import Clock, SystemClock
from ..ids import IdFactory
from ..store import JsonStore, JsonlJournal
from . import inventory, orders, reminders
from .events import EVENT_KINDS, WorkshopEvent, make_event
from .models import Part, Person, WorkOrder
from .errors import StockDrift, WorkshopError

_SNAPSHOT_DOC = "workshop/snapshot"

#: 提交校验闭包：拿到只读视角的当前状态，有问题就抛 WorkshopError。
Validator = Callable[["WorkshopView"], None]


class WorkshopView:
    """提交校验闭包看到的只读状态视角。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    @property
    def people(self) -> dict[str, Person]:
        return self._state["people"]

    @property
    def parts(self) -> dict[str, Part]:
        return self._state["parts"]

    @property
    def stock(self) -> dict:
        return self._state["stock"]

    @property
    def order_state(self) -> dict:
        return self._state["orders"]

    def order(self, order_id: str) -> dict | None:
        return orders.get(self._state["orders"], order_id)

    def person(self, person_id: str) -> Person | None:
        return self._state["people"].get(person_id)

    def part(self, code: str) -> Part | None:
        return self._state["parts"].get(code)


def _parses(line: str) -> bool:
    """一行流水是否能解析成 JSON（判断末尾坏行用）。"""

    try:
        json.loads(line)
        return True
    except json.JSONDecodeError:
        return False


def _blank_state() -> dict:
    return {
        "people": {},
        "parts": {},
        "stock": inventory.new_stock_state(),
        "orders": orders.new_order_state(),
        "reminders": {},
        "last_seq": 0,
    }


def _apply_catalog(state: dict, event: WorkshopEvent) -> None:
    if event.kind == "PERSON_REGISTERED":
        state["people"][str(event.data["person_id"])] = Person(
            id=str(event.data["person_id"]),
            name=str(event.data.get("name", "")),
            role=str(event.data.get("role", "")),
            shift=str(event.data.get("shift", "DAY")),
        )
    elif event.kind == "PART_REGISTERED":
        state["parts"][str(event.data["part_code"])] = Part(
            code=str(event.data["part_code"]),
            name=str(event.data.get("name", "")),
            unit=str(event.data.get("unit", "个")),
            location=str(event.data.get("location", "")),
        )
    elif event.kind == "REMINDER_RAISED":
        state["reminders"][str(event.data["order_id"])] = str(event.data["level"])


def _apply_event(state: dict, event: WorkshopEvent) -> None:
    """把一条事件投进所有相关投影。"""

    _apply_catalog(state, event)
    inventory.apply(state["stock"], event)
    orders.apply(state["orders"], event)


def _freeze_snapshot(state: dict) -> dict:
    snap = {
        "people": {key: value.as_dict() for key, value in state["people"].items()},
        "parts": {key: value.as_dict() for key, value in state["parts"].items()},
        "stock": copy.deepcopy(state["stock"]),
        "orders": copy.deepcopy(state["orders"]),
        "reminders": dict(state["reminders"]),
        "last_seq": state["last_seq"],
    }
    return snap


def _thaw_snapshot(payload: dict) -> dict:
    state = _blank_state()
    if not isinstance(payload, dict):
        raise StockDrift("快照文档结构非法")
    for key, raw in payload.get("people", {}).items():
        state["people"][key] = Person.from_dict(raw)
    for key, raw in payload.get("parts", {}).items():
        state["parts"][key] = Part.from_dict(raw)
    stock = payload.get("stock", {})
    if not isinstance(stock, dict):
        raise StockDrift("快照库存结构非法")
    state["stock"] = copy.deepcopy(stock)
    order_payload = payload.get("orders", {})
    if not isinstance(order_payload, dict) or "orders" not in order_payload:
        raise StockDrift("快照工单结构非法")
    state["orders"] = copy.deepcopy(order_payload)
    state["reminders"] = dict(payload.get("reminders", {}))
    state["last_seq"] = int(payload.get("last_seq", 0))
    return state


class WorkshopRegistry:
    """事件注册表（唯一持有可变状态的对象）。"""

    def __init__(
        self,
        store: JsonStore,
        journal: JsonlJournal,
        clock: Clock | None = None,
        notifier: Callable[[WorkshopEvent], None] | None = None,
    ) -> None:
        self._store = store
        self._journal = journal
        self._clock = clock or SystemClock()
        self._notifier = notifier
        self._ids = IdFactory("wo")
        self._lock = threading.RLock()
        self._state = _blank_state()
        self._bootstrap()

    # ---- 基础读取 -----------------------------------------------------

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def ids(self) -> IdFactory:
        return self._ids

    def view_state(self) -> dict:
        """只读内部状态（service 构建模型用；调用方不得改写）。"""

        with self._lock:
            return self._state

    def all_events(self) -> list[WorkshopEvent]:
        records = self._journal.read_all()
        return [WorkshopEvent.from_dict(record) for record in records]

    # ---- 启动恢复 -----------------------------------------------------

    def _load_journal_records(self) -> tuple[list[dict], int]:
        """读取流水，容忍末尾半条记录（提交中途掉电）。

        逐行解析：除末尾外任何一行解析失败都视为流水被破坏，直接报错；只有
        末尾连续的坏行才按"没写完"处理并截掉，返回 (记录列表, 截掉行数)。
        """

        good: list[dict] = []
        trailing_bad = 0
        segments = self._journal.segments()
        for index, path in enumerate(segments):
            last_segment = index == len(segments) - 1
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line for line in handle.read().splitlines() if line.strip()]
            segment_good: list[dict] = []
            for line_no, line in enumerate(lines):
                try:
                    segment_good.append(json.loads(line))
                    continue
                except json.JSONDecodeError:
                    pass
                suffix_all_bad = last_segment and all(
                    not _parses(candidate) for candidate in lines[line_no:]
                )
                if not suffix_all_bad:
                    raise StockDrift(
                        f"流水 {path} 第 {line_no + 1} 行损坏且后面还有完整记录，拒绝启动"
                    )
                trailing_bad += 1
            good.extend(segment_good)
        return good, trailing_bad

    def _truncate_bad_tail(self, bad_count: int) -> None:
        """把最后一段末尾没写完的坏行截掉（原子替换段文件）。"""

        path = self._journal.segments()[-1]
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        kept = lines[:-bad_count]
        temp = path + ".recover"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.writelines(line + "\n" for line in kept)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def _bootstrap(self) -> None:
        records, trailing_bad = self._load_journal_records()
        if trailing_bad:
            # 截掉没写完的尾巴，保证后续追加从完整行开始。
            self._truncate_bad_tail(trailing_bad)
        events = [WorkshopEvent.from_dict(record) for record in records]
        snapshot = self._store.read(_SNAPSHOT_DOC)
        rebuilt_from_journal = False
        if snapshot is not None:
            try:
                state = _thaw_snapshot(snapshot)
                tail = [event for event in events if event.seq > state["last_seq"]]
                for event in tail:
                    _apply_event(state, event)
                inventory.assert_consistent(state["stock"])
                state["reminders"].update(reminders.highest_raised(events))
                state["last_seq"] = events[-1].seq if events else 0
                self._state = state
                return
            except (StockDrift, KeyError, TypeError, ValueError) as error:
                # 快照损坏/账不平：不相信快照，退回流水重建，并留审计痕迹。
                rebuilt_from_journal = True
                rebuild_reason = str(error)
        # 无快照或快照不可信：从第 1 条事件全量重放。
        state = self._rebuild_state(events)
        self._state = state
        if rebuilt_from_journal or (events and snapshot is None):
            reason = rebuild_reason if rebuilt_from_journal else "快照缺失，从流水重建"
            self._append_rebuilt_event(reason)

    @staticmethod
    def _rebuild_state(events: list[WorkshopEvent]) -> dict:
        state = _blank_state()
        for event in events:
            _apply_catalog(state, event)
        # 两个投影各自提供重放入口，恢复路径与在线提交共用同一份投影逻辑。
        state["stock"] = inventory.rebuild(events)
        state["orders"] = orders.rebuild(events)
        # 重建时流水内部恒等式已由 inventory.rebuild 校验；这里不再重复。
        state["reminders"].update(reminders.highest_raised(events))
        state["last_seq"] = events[-1].seq if events else 0
        return state

    def _append_rebuilt_event(self, reason: str) -> None:
        """快照恢复后补一条重建审计事件（注意此时状态已经是重建结果）。"""

        event = make_event(
            "SNAPSHOT_REBUILT",
            self._clock.now(),
            "system",
            {"reason": reason[:300]},
        )
        event = WorkshopEvent(
            id=event.id,
            seq=self._state["last_seq"] + 1,
            kind=event.kind,
            at=event.at,
            actor=event.actor,
            data=event.data,
        )
        self._journal.append(event.as_dict())
        self._state["last_seq"] = event.seq
        self._persist_snapshot()

    # ---- 提交 ---------------------------------------------------------

    def build_event(self, kind: str, actor: str, data: dict) -> WorkshopEvent:
        """构造候选事件（at 取当前时钟；seq 提交时才分配）。"""

        return make_event(kind, self._clock.now(), actor, data)

    def commit(self, candidates: Iterable[WorkshopEvent] | Callable[[WorkshopView], Iterable[WorkshopEvent]], validator: Validator | None = None) -> list[WorkshopEvent]:
        """原子提交一批事件。

        ``candidates`` 可以是事件列表，也可以是一个构造函数：拿到提交前状态
        后在锁内返回事件列表（关单释放量等必须按最新台账计算的数据走这条路，
        避免"锁外算好、提交时已过期"）。

        校验和试投影都在锁内完成；任何异常都会把现场原样保留，流水一条都
        不会多。返回已提交（带 seq）的事件列表。
        """

        with self._lock:
            # 先在提交前状态上做前置校验（权限优先于状态流转判定），通过后才
            # 允许在锁内基于同一份状态构造事件，避免"用过期数据算释放量"。
            if validator is not None:
                validator(WorkshopView(self._state))
            if callable(candidates):
                built = list(candidates(WorkshopView(self._state)))
            else:
                built = list(candidates)
            for event in built:
                if event.kind not in EVENT_KINDS:
                    raise WorkshopError(f"未知事件类型，拒绝落盘: {event.kind}")
            trial = copy.deepcopy(self._state)
            for event in built:
                _apply_event(trial, event)
            inventory.assert_consistent(trial["stock"])
            # 校验通过：分配序号、顺序落盘（fsync 在 JsonlJournal 内）。
            committed: list[WorkshopEvent] = []
            next_seq = self._state["last_seq"]
            try:
                for event in built:
                    next_seq += 1
                    stamped = WorkshopEvent(
                        id=event.id,
                        seq=next_seq,
                        kind=event.kind,
                        at=event.at,
                        actor=event.actor,
                        data=event.data,
                    )
                    self._journal.append(stamped.as_dict())
                    committed.append(stamped)
            except Exception:
                # 流水写了一半：内存状态绝不能前进。重启时以流水实际内容为准，
                # 快照不更新，调用方看到异常后可安全重试（事件 id 不同，无副作用）。
                raise
            self._state = trial
            self._state["last_seq"] = next_seq
            self._persist_snapshot()
            for event in committed:
                if self._notifier is not None and event.kind == "REMINDER_RAISED":
                    self._notifier(event)
            return committed

    def _persist_snapshot(self) -> None:
        self._store.write(_SNAPSHOT_DOC, _freeze_snapshot(self._state))

    # ---- 对账与恢复（运维入口）---------------------------------------

    def verify(self) -> list[str]:
        """把快照台账与流水全量重算结果比对，返回差异描述（空为账平）。"""

        with self._lock:
            journal_events = [
                WorkshopEvent.from_dict(record) for record in self._load_journal_records()[0]
            ]
            rebuilt = self._rebuild_state(journal_events)
            problems = inventory.check_invariants(self._state["stock"])
            live_stock = self._state["stock"]
            rebuilt_stock = rebuilt["stock"]
            codes = set(live_stock) | set(rebuilt_stock)
            for code in sorted(codes):
                live = live_stock.get(code, {})
                gold = rebuilt_stock.get(code, {})
                for column in inventory.LEDGER_COLUMNS:
                    if live.get(column, 0) != gold.get(column, 0):
                        problems.append(
                            f"{code}: {column} 快照 {live.get(column, 0)} != 流水重算 {gold.get(column, 0)}"
                        )
            return problems

    def rebuild_from_journal(self, actor: str, note: str = "") -> list[str]:
        """账实不符恢复：以流水为准重建全部投影，并审计记录发现的差异。"""

        with self._lock:
            problems = self.verify()
            journal_events = [
                WorkshopEvent.from_dict(record) for record in self._load_journal_records()[0]
            ]
            self._state = self._rebuild_state(journal_events)
            self._persist_snapshot()
            if problems:
                event = self.build_event(
                    "SNAPSHOT_REBUILT",
                    actor,
                    {"reason": "手动重建", "found": problems[:20], "note": note[:300]},
                )
                committed = self.commit([event])
                return [str(item) for item in committed]
            return []

    # ---- 便捷查询 -----------------------------------------------------

    def get_order_view(self, order_id: str) -> WorkOrder | None:
        with self._lock:
            raw = orders.get(self._state["orders"], order_id)
            return orders.view(raw) if raw else None

    def list_order_views(self) -> list[WorkOrder]:
        with self._lock:
            return orders.views(self._state["orders"])
