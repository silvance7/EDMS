"""SQLite 连接与事务管理。

这是整个应用里**唯一**直接持有 sqlite3.Connection 的地方。
domain 层不 import sqlite3，只通过本模块的 transaction() / query() 工作。

几个关键 PRAGMA 的取舍：
    journal_mode = WAL   —— 读写不互相阻塞。画板子时一边查一边录入不会互相卡。
    foreign_keys = ON    —— SQLite 默认是关的！不开树形结构的级联删除就是摆设。
    synchronous = NORMAL —— WAL 模式下这个档位足够安全，且写性能明显好于 FULL。
    temp_store = MEMORY  —— 排序/临时表走内存，列表页排序更快。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from app.paths import data_dir, resolve_resource

# 数据库固定放在"exe（或项目根）旁边的 data/"。为什么不放程序资源目录里：
# 打包后那里是临时目录或 _internal，数据会丢。详见 app/paths.py 的说明。
DEFAULT_DB_PATH = data_dir() / "inventory.db"
SCHEMA_PATH = resolve_resource("storage", "schema.sql")


class Database:
    """一个应用实例只应该建一个 Database，全局共享（单进程单用户）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None 表示关闭 sqlite3 模块的隐式事务管理，
        # 由我们自己用 BEGIN/COMMIT 显式控制——出库扣减必须和记流水在同一个事务里。
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()

    # -- 基础设施 ----------------------------------------------------------

    def _apply_pragmas(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA synchronous = NORMAL")
            cur.execute("PRAGMA temp_store = MEMORY")
        finally:
            cur.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def initialize(self) -> None:
        """建表（幂等）。已存在的表不会被改动。"""
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- 查询 / 执行 --------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        cur = self._conn.execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        cur = self._conn.execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """单条写操作。返回 lastrowid（INSERT）或受影响行数（UPDATE/DELETE）。"""
        cur = self._conn.execute(sql, params)
        try:
            return cur.lastrowid if cur.lastrowid is not None else cur.rowcount
        finally:
            cur.close()

    def execute_many(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        self._conn.executemany(sql, seq)

    # -- 事务 --------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务边界。

        Service 层凡是"一次操作要改多张表"的场景（出库扣减 + 记流水、
        入库建批次 + 记流水、删除器件级联清理）都必须包在这里面，
        保证要么全成、要么全滚，不会留下对不上的数据。
        """
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- 生命周期 ----------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def backup_to(self, target: str | Path) -> Path:
        """在线热备份。

        SQLite 官方推荐的备份方式，即使此刻有其它连接在读也不会拿到半截数据。
        比直接复制 .db 文件安全——WAL 模式下还有 -wal / -shm 文件，
        单纯拷 .db 可能丢失最近事务。
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            self._conn.backup(dest)
        finally:
            dest.close()
        return target
