"""车间域异常。

沿用控制服务 :class:`RailError` 的携带组件名约定。调用方按异常类型就能区分
"谁都没权限""状态不对""备件不够""账实不符"这几类现场处置方式完全不同的失败。
"""

from __future__ import annotations

from ..errors import RailError


class WorkshopError(RailError):
    """车间域异常基类。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, component="workshop")


class PermissionDenied(WorkshopError):
    """操作人角色不允许执行该动作。"""


class UnknownPerson(WorkshopError):
    """人员标识未登记。"""


class InvalidTransition(WorkshopError):
    """工单状态流转或门区联锁条件不满足。"""


class InventoryShortage(WorkshopError):
    """预占或领料时可用库存不足，整批操作必须回滚。"""


class OrderClosed(WorkshopError):
    """工单已终结（已关单或已废单），历史不可变。"""


class StockDrift(WorkshopError):
    """快照台账与流水重算结果不一致，或盘点差异超阈值。"""
