"""门区报修工单与备件管理子系统。

纸质流程对不上账的根因是三件事没人同时保证：工单状态、备件库存、门的锁定。
本子系统用一个 SQLite 库把三者放进同一批事务里，并留下三类**只追加**历史：

- ``wo_events``       工单事件流（工单的真相，触发器禁改禁删）；
- ``inventory_ledger`` 实物库存流水（库存的真相，正负流水可重算库存）；
- ``audit_entries``   管理与权限审计（谁在什么时候做了什么）。

备件账采用「开单预占、领料实扣」的混合模型：

- 开单时校验可用量（``on_hand - reserved_qty``）并预占，不动实物；
- 领料时在同一事务里扣实物、减预占、写流水，任一备件不足则整笔回滚；
- 关单时未领用的预占自动释放；废单时未领预占自动释放，**已领部分不自动回补**，
  生成待退料任务，由库管人工确认退回（件可能已被用掉或损坏，自动回补会造假账）。

门的维修锁与工单状态同生共死：开单即锁、关单或废单才解锁；同一樘门同时只允许
一张活动工单（部分唯一索引强制），从存储层杜绝「同一扇门开两张单」。
"""

from __future__ import annotations

from .api import MaintenanceApi
from .db import Database
from .errors import (
    ConflictError,
    MaintenanceError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .notify import Notifier, RecordingNotifier, Reminder
from .schema import SCHEMA_SQL, bootstrap
from .service import MaintenanceService
from .service import (
    MANAGER,
    REPAIR,
    REPORTER,
    STOREKEEPER,
    ROLES,
)

__all__ = [
    "MaintenanceApi",
    "MaintenanceService",
    "Database",
    "Notifier",
    "RecordingNotifier",
    "Reminder",
    "MaintenanceError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "PermissionDenied",
    "SCHEMA_SQL",
    "bootstrap",
    "ROLES",
    "REPORTER",
    "REPAIR",
    "STOREKEEPER",
    "MANAGER",
]
