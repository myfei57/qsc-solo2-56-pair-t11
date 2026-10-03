"""超期提醒分级、升级与去重。

规则：

* 活动工单超过 ``due_at`` 即超期；扫描器 :func:`evaluate` 只返回当前应该落
  一条什么级别的提醒，是否真的落盘由注册表对照"该单已提醒到的最高级别"决定，
  保证同一级别不重复骚扰负责人（去重）。
* 分级（按超期时长，自 due_at 起算）：
    L1 逾期即时提醒负责人本人；
    L2 逾期后再等 ``l2_after`` 小时仍未终结，抄报班组/调度；
    L3 再等 ``l3_after`` 小时仍未终结，升级主管。
* 终结工单不再产生提醒；时钟来自可注入的 :class:`~psdline.clock.Clock`，
  演示和测试用 ManualClock 不真实等待即可复现跨班超期。
"""

from __future__ import annotations

from dataclasses import dataclass

LEVEL_1 = "L1"
LEVEL_2 = "L2"
LEVEL_3 = "L3"

#: 级别序号，用于比较"已提醒到哪一级"。
LEVEL_ORDER = {LEVEL_1: 1, LEVEL_2: 2, LEVEL_3: 3}

#: 各级别的默认通知对象角色（具体人由服务层按工单 owner/排班解析）。
LEVEL_AUDIENCE = {
    LEVEL_1: ("owner",),
    LEVEL_2: ("owner", "dispatch"),
    LEVEL_3: ("owner", "dispatch", "supervisor"),
}

_HOUR = 3600.0


@dataclass(frozen=True)
class ReminderPolicy:
    """提醒分级策略（阈值单位：小时）。"""

    l2_after: float = 4.0
    l3_after: float = 24.0

    def overdue_seconds(self, order, now: float) -> float:
        """已超期秒数；未超期或已终结为 0。"""

        if order.terminal or now <= order.due_at:
            return 0.0
        return now - order.due_at

    def evaluate(self, order, last_level: str, now: float) -> str:
        """返回此刻应补发的新级别；无需提醒或无需升级时返回空串。

        ``last_level`` 为该单历史上已落盘的最高提醒级别。
        """

        overdue = self.overdue_seconds(order, now)
        if overdue <= 0:
            return ""
        if overdue >= (self.l2_after + self.l3_after) * _HOUR:
            wanted = LEVEL_3
        elif overdue >= self.l2_after * _HOUR:
            wanted = LEVEL_2
        else:
            wanted = LEVEL_1
        if LEVEL_ORDER.get(last_level, 0) >= LEVEL_ORDER[wanted]:
            return ""
        return wanted


def highest_raised(events: list) -> dict[str, str]:
    """从提醒事件重放每个工单已提醒到的最高级别。"""

    highest: dict[str, str] = {}
    for event in events:
        if event.kind != "REMINDER_RAISED":
            continue
        order_id = str(event.data.get("order_id", ""))
        level = str(event.data.get("level", ""))
        if LEVEL_ORDER.get(level, 0) > LEVEL_ORDER.get(highest.get(order_id, ""), 0):
            highest[order_id] = level
    return highest
