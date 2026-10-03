"""工单备件子系统的 HTTP 接口。

与主控制台接口同构：``/api/maintain/...`` 翻译成 :class:`MaintenanceService`
调用，领域异常按类型映射状态码。与主接口不同的是这里每个动作都有「操作者」，
从请求头 ``X-User-Id`` 或请求体 ``actor`` 字段取（头优先），没有身份一律
``401``——关单、废单这种权限敏感动作不能匿名。
"""

from __future__ import annotations

from typing import Any, Callable

from ..console.router import Router
from .errors import (
    ConflictError,
    MaintenanceError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .service import MaintenanceService

Handler = Callable[[dict, dict, str], dict]


class MaintenanceApi:
    """把 HTTP 请求翻译成工单备件服务调用。"""

    def __init__(self, service: MaintenanceService) -> None:
        self._service = service
        self._router = Router()
        self._register()

    @property
    def service(self) -> MaintenanceService:
        return self._service

    def _add(self, method: str, pattern: str, handler: Handler) -> None:
        # 复用主工程 Router：handler 签名是 (params, body)，这里包一层注入 actor。
        def routed(params: dict, body: dict) -> dict:
            actor = str(params.pop("__actor__", body.get("actor", "")) or "")
            return handler(params, body, actor)

        self._router.add(method, pattern, routed)

    def _register(self) -> None:
        add = self._add
        svc = self._service

        # ---- 档案 -------------------------------------------------------
        add("POST", "/api/maintain/users", lambda p, b, a: svc.create_user(
            a, _require(b, "user_id"), _require(b, "name"), _require(b, "role")))
        add("GET", "/api/maintain/users/{userID}", lambda p, b, a: svc.get_user(p["userID"]))
        add("POST", "/api/maintain/doors", lambda p, b, a: svc.create_door(
            a, _require(b, "door_id"), _require(b, "name"), str(b.get("location", ""))))
        add("GET", "/api/maintain/doors", lambda p, b, a: {"doors": svc.list_doors()})
        add("GET", "/api/maintain/doors/{doorID}", lambda p, b, a: svc.get_door(p["doorID"]))
        add("POST", "/api/maintain/parts", lambda p, b, a: svc.create_part(
            a, _require(b, "sku"), _require(b, "name"), int(b.get("initial_qty", 0)),
            str(b.get("unit", "件"))))
        add("GET", "/api/maintain/parts", lambda p, b, a: {"parts": svc.list_parts()})
        add("GET", "/api/maintain/parts/{sku}", lambda p, b, a: svc.get_part(p["sku"]))
        add("POST", "/api/maintain/receive", lambda p, b, a: svc.receive_stock(
            a, _require(b, "sku"), int(_require(b, "qty")), str(b.get("note", ""))))

        # ---- 工单 -------------------------------------------------------
        add("POST", "/api/maintain/work-orders", self._open)
        add("GET", "/api/maintain/work-orders", self._list_wo)
        add("GET", "/api/maintain/work-orders/{woID}", self._get_wo)
        add("GET", "/api/maintain/work-orders/{woID}/history",
            lambda p, b, a: {"history": svc.work_order_history(p["woID"])})
        add("POST", "/api/maintain/work-orders/{woID}/issue", self._issue)
        add("POST", "/api/maintain/work-orders/{woID}/start", self._start)
        add("POST", "/api/maintain/work-orders/{woID}/complete", self._complete)
        add("POST", "/api/maintain/work-orders/{woID}/close", self._close)
        add("POST", "/api/maintain/work-orders/{woID}/reassign", self._reassign)
        add("POST", "/api/maintain/work-orders/{woID}/void", self._void)

        # ---- 退料 / 盘点 / 提醒 / 核对 ----------------------------------
        add("GET", "/api/maintain/return-tasks",
            lambda p, b, a: {"tasks": svc.list_return_tasks(_truthy(p.get("pending_only", "1")))})
        add("POST", "/api/maintain/return-tasks/{taskID}/confirm", self._confirm_return)
        add("POST", "/api/maintain/stocktakes", self._create_stocktake)
        add("GET", "/api/maintain/stocktakes",
            lambda p, b, a: {"stocktakes": svc.list_stocktakes(p.get("status"))})
        add("POST", "/api/maintain/stocktakes/{takeID}/decide", self._decide_stocktake)
        add("POST", "/api/maintain/scan-overdue",
            lambda p, b, a: {"reminders": svc.scan_overdue()})
        add("GET", "/api/maintain/reminders",
            lambda p, b, a: {"reminders": svc.list_reminders(p.get("wo_id"))})
        add("GET", "/api/maintain/verify", lambda p, b, a: svc.verify_consistency())
        add("POST", "/api/maintain/rebuild", lambda p, b, a: svc.rebuild_projections(a))
        add("GET", "/api/maintain/ledger",
            lambda p, b, a: {"ledger": svc.ledger(p.get("sku"), int(p.get("limit", 200)))})
        add("GET", "/api/maintain/audit",
            lambda p, b, a: {"entries": svc.audit_trail(p.get("kind"), int(p.get("limit", 200)))})

    def routes(self) -> list[dict]:
        return [route.as_dict() for route in self._router.routes()]

    def handle(
        self, method: str, path: str, query: dict, body: dict, headers: dict | None = None
    ) -> tuple[int, dict]:
        """处理一次请求，返回状态码与响应体。"""

        matched = self._router.match(method, path)
        if matched is None:
            return 404, {"error": "not_found", "path": path}
        handler, params = matched
        params = {**query, **params}
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        actor = headers.get("x-user-id") or body.get("actor")
        if not actor:
            return 401, {"error": "unauthorized", "message": "缺少操作者身份（X-User-Id 头或 actor 字段）"}
        params["__actor__"] = str(actor)
        try:
            payload = handler(params, body)
        except PermissionDenied as error:
            return 403, {"error": type(error).__name__, **error.as_record()}
        except NotFoundError as error:
            return 404, {"error": type(error).__name__, **error.as_record()}
        except ConflictError as error:
            return 409, {"error": type(error).__name__, **error.as_record()}
        except (ValidationError, MaintenanceError) as error:
            return 400, {"error": type(error).__name__, **error.as_record()}
        except (TypeError, ValueError) as error:
            return 400, {"error": "bad_request", "message": str(error)}
        return 200, payload

    # ---- 请求翻译 --------------------------------------------------------

    @staticmethod
    def _lines(body: dict, key: str = "items") -> dict[str, int]:
        raw = body.get(key)
        if not isinstance(raw, list) or not raw:
            raise ValidationError(f"字段 {key!r} 必须是非空数组，元素为 {{sku, qty}}")
        lines: dict[str, int] = {}
        for item in raw:
            if not isinstance(item, dict) or "sku" not in item or "qty" not in item:
                raise ValidationError("备件行必须包含 sku 与 qty")
            lines[str(item["sku"])] = int(item["qty"])
        return lines

    def _open(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.open_work_order(
            actor,
            _require(body, "door_id"),
            _require(body, "title"),
            self._lines(body, "parts") if body.get("parts") else None,
            owner_id=body.get("owner_id"),
            description=str(body.get("description", "")),
            sla_hours=float(body["sla_hours"]) if body.get("sla_hours") is not None else None,
            shift=str(body.get("shift", "")),
        )

    def _list_wo(self, params: dict, body: dict, actor: str) -> dict:
        status = params.get("status")
        return {"work_orders": self._service.list_work_orders(status)}

    def _get_wo(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.get_work_order(params["woID"])

    def _issue(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.issue_parts(
            actor, params["woID"], self._lines(body), note=str(body.get("note", "")))

    def _start(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.start_work(
            actor, params["woID"], shift=body.get("shift") if "shift" in body else None)

    def _complete(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.complete_work(
            actor, params["woID"], str(body.get("resolution", "")))

    def _close(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.close_work_order(actor, params["woID"])

    def _reassign(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.reassign(
            actor, params["woID"], _require(body, "new_owner_id"),
            shift=str(body.get("shift", "")), note=str(body.get("note", "")))

    def _void(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.void_work_order(
            actor, params["woID"], str(body.get("reason", "")))

    def _confirm_return(self, params: dict, body: dict, actor: str) -> dict:
        qty = int(body["qty"]) if body.get("qty") is not None else None
        return self._service.confirm_return(
            actor, params["taskID"], qty, note=str(body.get("note", "")))

    def _create_stocktake(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.create_stocktake(
            actor, _require(body, "sku"), int(_require(body, "counted_qty")),
            note=str(body.get("note", "")))

    def _decide_stocktake(self, params: dict, body: dict, actor: str) -> dict:
        return self._service.decide_stocktake(
            actor, params["takeID"], bool(body.get("approve", True)),
            note=str(body.get("note", "")))


def _require(body: dict, key: str) -> Any:
    value = body.get(key)
    if value is None or str(value) == "":
        raise ValidationError(f"缺少必填字段 {key!r}")
    return value


def _truthy(value: str | None) -> bool:
    return str(value).lower() not in ("0", "false", "no", "")
