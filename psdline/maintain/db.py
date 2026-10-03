"""SQLite 连接与事务封装。

并发语义是这套系统的生命线，连接初始化时固定打开四件事：

- ``foreign_keys=ON`` 外键强制；
- ``journal_mode=WAL`` 读写不互相阻塞；
- ``busy_timeout`` 抢锁时等待而不是立刻报 ``database is locked``；
- 所有写事务走 :meth:`transaction` 里的 ``BEGIN IMMEDIATE``——一进事务就拿写锁，
  「先读后写」的经典竞态（两个线程同时读到 10 件库存然后都扣 8 件）在锁等待层
  串行化，配合条件更新做二次校验，库存不可能扣成负数。

每个线程持有自己的连接（SQLite 连接不能跨线程共享），连接按线程懒创建。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .schema import bootstrap


class Database:
    """线程本地的 SQLite 连接池（每线程一连接）。"""

    def __init__(self, path: str, *, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        if path == ":memory:":
            # 内存库多连接互不可见，测试里用单连接共享缓存。
            self._shared: sqlite3.Connection | None = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._configure(self._shared)
            bootstrap(self._shared)
            self._initialized = True
        else:
            self._shared = None
        self.ensure_schema()

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        # autocommit 模式：事务边界由 transaction() 里的 BEGIN IMMEDIATE 显式管理。
        conn.isolation_level = None
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000.0)
        self._configure(conn)
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def connect(self) -> sqlite3.Connection:
        """返回当前线程的连接（懒创建）。"""

        if self._shared is not None:
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def ensure_schema(self) -> None:
        """幂等建表（文件库只执行一次，靠锁与标志位防重复）。"""

        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            conn = self.connect()
            bootstrap(conn)
            self._initialized = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """开启一个立即写事务，正常退出提交、异常回滚。

        ``BEGIN IMMEDIATE`` 一进来就持有写锁：事务内读到的库存与工单状态在提交前
        不会被别的写事务改变（其它写事务阻塞在 ``BEGIN IMMEDIATE`` 上），这正是
        「并发领料不超卖」需要的保证。
        """

        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        """取一行，没有时返回 ``None``。"""

        return self.connect().execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """取全部行。"""

        return list(self.connect().execute(sql, params).fetchall())

    def close(self) -> None:
        """关闭当前线程持有的连接。"""

        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._shared is not None:
            self._shared.close()
            self._shared = None

    # ---- 供恢复使用的底层开关（恢复工具用） ---------------------------

    def executescript(self, script: str) -> None:  # pragma: no cover - 仅恢复工具
        self.connect().executescript(script)
