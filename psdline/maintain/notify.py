"""超期提醒出口。

服务层只依赖 :class:`Notifier` 抽象，生产环境可以接短信/企业微信/邮件；标定与
验证环境用 :class:`RecordingNotifier` 把提醒留在内存里，便于断言「提醒了谁、
几级、为什么」。提醒落库（``reminders`` 表，只追加）由服务层负责，Notifier 只
管投递，两层分开后「重复扫描」不会重复通知：落库的唯一约束
``UNIQUE(wo_id, level)`` 先去重，再投递。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Reminder:
    """一次超期提醒。"""

    wo_id: str
    level: int
    target_user_id: str
    kind: str  # due（超过 SLA 未完工）/ done_unclosed（完工未关单、门仍锁）
    at: float
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "wo_id": self.wo_id,
            "level": self.level,
            "target_user_id": self.target_user_id,
            "kind": self.kind,
            "at": self.at,
            "note": self.note,
        }


class Notifier(Protocol):
    """提醒投递协议。"""

    def send(self, reminder: Reminder) -> None:
        """投递一条提醒。"""


@dataclass
class RecordingNotifier:
    """把提醒记录在内存里的假投递器（测试与演示用）。"""

    sent: list[Reminder] = field(default_factory=list)

    def send(self, reminder: Reminder) -> None:
        self.sent.append(reminder)

    def targets(self) -> list[str]:
        """收到过提醒的用户标识（按发送顺序）。"""

        return [item.target_user_id for item in self.sent]
