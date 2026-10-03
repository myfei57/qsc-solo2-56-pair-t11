"""工单备件子系统命令行入口。

子命令：

- ``serve``  启动工单备件 HTTP 服务（默认 127.0.0.1:8090）；
- ``demo``   在临时库上跑完整业务流程演示（含并发与权限场景）；
- ``scan``   对指定库执行一次超期扫描（可挂进定时任务/cron）；
- ``verify`` 对指定库做账实一致性核对，发现偏差以非零码退出。
"""

from __future__ import annotations

import argparse
import json
import sys

from ..clock import SystemClock
from .db import Database
from .demo import run_demo
from .notify import RecordingNotifier
from .server import MaintenanceServer
from .service import MaintenanceService


def _dump(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _cmd_serve(args: argparse.Namespace) -> int:
    db = Database(args.db)
    service = MaintenanceService(db, clock=SystemClock(), notifier=RecordingNotifier())
    server = MaintenanceServer(service, host=args.host, port=args.port)
    base_url = server.start()
    _dump({
        "listening": base_url,
        "db": args.db,
        "routes": server.api.routes(),
        "hint": "请求动作接口需带 X-User-Id 头（种子账号 u-report/u-repair/u-store/u-admin）",
    })
    if args.duration > 0:
        import time

        try:
            time.sleep(args.duration)
        except KeyboardInterrupt:  # pragma: no cover - 交互路径
            pass
        server.stop()
        return 0
    try:
        while True:
            import time

            time.sleep(1.0)
    except KeyboardInterrupt:  # pragma: no cover - 交互路径
        server.stop()
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    result = run_demo(args.db)
    failed = [
        key for key, value in result.items()
        if isinstance(value, bool) and not value and key in {
            "duplicate_open_blocked", "partial_rollback_ok",
            "denied_issue_for_reporter", "door_locked_after_complete",
            "door_unlocked_after_close", "stocktake_self_approve_denied",
            "history_tamper_rejected",
        }
    ]
    if failed:
        print(f"演示断言失败: {failed}", file=sys.stderr)
        return 1
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    db = Database(args.db)
    service = MaintenanceService(db, clock=SystemClock(), notifier=RecordingNotifier())
    reminders = service.scan_overdue()
    _dump({"reminders": reminders, "count": len(reminders)})
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    db = Database(args.db)
    service = MaintenanceService(db, clock=SystemClock())
    report = service.verify_consistency()
    _dump(report)
    return 0 if report["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m psdline.maintain",
                                     description="门区报修工单与备件管理")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--db", default="var/state/maintain.db")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    serve.add_argument("--duration", type=float, default=0.0,
                       help="运行秒数后自动退出（0 表示常驻）")
    serve.set_defaults(func=_cmd_serve)

    demo = sub.add_parser("demo", help="跑完整业务流程演示")
    demo.add_argument("--db", default=None, help="指定数据库路径（默认临时库）")
    demo.set_defaults(func=_cmd_demo)

    scan = sub.add_parser("scan", help="执行一次超期扫描")
    scan.add_argument("--db", default="var/state/maintain.db")
    scan.set_defaults(func=_cmd_scan)

    verify = sub.add_parser("verify", help="账实一致性核对")
    verify.add_argument("--db", default="var/state/maintain.db")
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
