"""工单备件子系统的独立 HTTP 服务。

刻意与主控制台 :mod:`psdline.console.server` 分开部署：维修工单要写事务型
SQLite，和联锁控制台的 JSON 文档存储互不影响，但两者风格一致（同样的
``ThreadingHTTPServer``、同样的 JSON 约定）。多线程请求由服务层的
``BEGIN IMMEDIATE`` 串行化写事务，压测并发领料也不会超卖。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .api import MaintenanceApi
from .db import Database
from .service import MaintenanceService


class _Handler(BaseHTTPRequestHandler):
    server_version = "psdline-maintain/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # 访问日志由审计/事件承载，不刷 stderr

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        headers = {"X-User-Id": self.headers.get("X-User-Id", "")}
        status, payload = self.server.api.handle(  # type: ignore[attr-defined]
            method, parsed.path, query, body, headers
        )
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _MaintainHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], api: MaintenanceApi) -> None:
        super().__init__(address, _Handler)
        self.api = api


class MaintenanceServer:
    """后台线程里运行的工单备件 HTTP 服务。"""

    def __init__(
        self,
        service: MaintenanceService,
        host: str = "127.0.0.1",
        port: int = 8090,
    ) -> None:
        self._service = service
        self._host = host
        self._port = port
        self._api = MaintenanceApi(service)
        self._server: _MaintainHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def api(self) -> MaintenanceApi:
        return self._api

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_address[1])

    def start(self) -> str:
        if self._server is not None:
            return self.base_url()
        self._server = _MaintainHTTPServer((self._host, self._port), self._api)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="psdline-maintain", daemon=True
        )
        self._thread.start()
        return self.base_url()

    def base_url(self) -> str:
        host = self._host if self._host not in ("0.0.0.0", "") else "127.0.0.1"
        return f"http://{host}:{self.port}"

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
