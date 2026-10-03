"""报修工单与备件管理（门区维修车间域）。

领域边界：

* 门区报修开单，开单即对门区上锁，修完关单（或废单）才解锁；
* 备件"开单预占、领料实扣"两阶段，跨班领料记在同一张工单上；
* 废单时未领预占自动回补，已领件必须退库或主管核销后才能废单；
* 所有状态都是只追加事件流的投影，工单历史不可变，快照可从流水重建对账。

模块划分：

``errors``     领域异常（权限、状态流转、库存不足、账实不符等）
``models``     角色、人员、备件、工单等只读视图数据类
``events``     车间事件定义与序列化
``inventory``  库存投影：预占/在库/已出库的台账恒等式
``orders``     工单生命周期投影与状态判定
``lockout``    门区锁定投影
``registry``   事件注册表：提交、快照、回放、对账、崩溃恢复
``reminders``  超期提醒分级与升级
``service``    对外门面：权限校验 + 事务提交
``demo``       端到端演示脚本
"""

from __future__ import annotations

from .errors import (
    InvalidTransition,
    InventoryShortage,
    OrderClosed,
    PermissionDenied,
    StockDrift,
    UnknownPerson,
    WorkshopError,
)
from .events import EVENT_KINDS, WorkshopEvent
from .models import OrderLine, Part, Person, WorkOrder
from .registry import WorkshopRegistry
from .reminders import ReminderPolicy
from .service import WorkshopService

__all__ = [
    "EVENT_KINDS",
    "InvalidTransition",
    "InventoryShortage",
    "OrderClosed",
    "OrderLine",
    "Part",
    "PermissionDenied",
    "Person",
    "ReminderPolicy",
    "StockDrift",
    "UnknownPerson",
    "WorkOrder",
    "WorkshopError",
    "WorkshopEvent",
    "WorkshopRegistry",
    "WorkshopService",
]
