"""工单备件子系统的异常类型。

和主工程 :class:`~psdline.errors.RailError` 平行（复用 ``component`` 习惯），
再按 HTTP 语义细分四类，接口层可以直接映射成状态码：

- :class:`ValidationError`   请求本身不合法（400）；
- :class:`NotFoundError`     工单、备件、门或用户不存在（404）；
- :class:`ConflictError`     状态机冲突、库存不足、门已被占用（409）；
- :class:`PermissionDenied` 当前角色无权执行该动作（403）。
"""

from __future__ import annotations


class MaintenanceError(Exception):
    """所有工单备件异常的基类，携带组件名。"""

    def __init__(self, message: str, *, component: str = "maintain") -> None:
        super().__init__(message)
        self.message = message
        self.component = component

    def as_record(self) -> dict:
        """转成审计流水可以直接落盘的一条记录。"""

        return {
            "type": type(self).__name__,
            "component": self.component,
            "message": self.message,
        }


class ValidationError(MaintenanceError):
    """参数非法（缺字段、数量非正、备件行重复等）。"""


class NotFoundError(MaintenanceError):
    """引用的实体不存在。"""


class ConflictError(MaintenanceError):
    """当前状态不允许该动作（库存不足、状态机不允许、门已被占用）。"""


class PermissionDenied(MaintenanceError):
    """当前用户角色无权执行该动作。"""
