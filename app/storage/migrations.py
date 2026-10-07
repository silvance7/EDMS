"""schema 版本管理。

为什么不引 Alembic：本项目只有一个 SQLite 文件、单用户、无并发迁移，
迁移框架的维护成本高于收益。用 meta.schema_version 加一个有序步骤列表，
够用、可控、出错能一眼看明白。

首次运行：schema.sql 全量建表（幂等），版本记为 1。
以后改表：往 MIGRATIONS 追加一条 (版本号, SQL)，**不要改动历史条目**。
"""

from __future__ import annotations

from collections.abc import Callable

from app.storage.db import Database

CURRENT_SCHEMA_VERSION = 2


def _has_column(db: Database, table: str, column: str) -> bool:
    return column in {r["name"] for r in db.query(f"PRAGMA table_info({table})")}


def _migrate_v2(db: Database) -> None:
    """v2：厂商从「器件」挪到「库存批次」。

    动机：同一个 10kΩ 电阻，YAGEO 买的和厚声买的是**同一个器件**，
    不该因为厂商不同就新建一条器件记录。厂商是「这一次采购」的属性，
    跟供应商一样属于批次层。

    老库：新增 stock_lot.manufacturer -> 把 part.manufacturer 灌进各批次 -> 删掉列。
    新库：schema.sql 已经是新结构，这里的判断直接跳过。
    """
    if not _has_column(db, "part", "manufacturer"):
        return

    with db.transaction() as conn:
        if not _has_column(db, "stock_lot", "manufacturer"):
            conn.execute(
                "ALTER TABLE stock_lot ADD COLUMN manufacturer TEXT NOT NULL DEFAULT ''"
            )
        # 原有的厂商信息不能丢：灌到该器件名下的每一条批次上
        conn.execute(
            """
            UPDATE stock_lot
               SET manufacturer = COALESCE(
                     (SELECT p.manufacturer FROM part p WHERE p.id = stock_lot.part_id), '')
             WHERE manufacturer = ''
            """
        )
        # 视图引用了 part.manufacturer，必须先删掉才动得了列
        conn.execute("DROP VIEW IF EXISTS v_part_overview")
        conn.execute("ALTER TABLE part DROP COLUMN manufacturer")


def _apply(db: Database, step: str | Callable[[Database], None] | None) -> None:
    if step is None:
        return
    if callable(step):
        step(db)
    elif step.strip():
        db.conn.executescript(step)


# (版本号, 增量 SQL 或迁移函数)。v1 的表结构由 schema.sql 负责，这里留空。
#
# 用**可调用对象**而不只是 SQL 字符串：有些迁移得先探测当前结构再决定做什么 ——
# 新库由 schema.sql 直接建成最新结构，老库才需要 ALTER。
# 写成纯 SQL 的话，在新库上会因为"列不存在"直接报错。
MIGRATIONS: list[tuple[int, str | Callable[[Database], None]]] = [
    (1, ""),
    (2, _migrate_v2),
]


def get_version(db: Database) -> int:
    row = db.query_one("SELECT value FROM meta WHERE key = 'schema_version'")
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def set_version(db: Database, version: int) -> None:
    db.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def run_migrations(db: Database) -> int:
    """建表 + 升级到最新版本号。幂等，可重复调用。返回最终版本号。"""
    db.initialize()

    version = get_version(db)
    for target, step in MIGRATIONS:
        if target <= version:
            continue
        _apply(db, step)
        version = target

    # 再跑一遍建表脚本：里面那句 DROP VIEW + CREATE VIEW 会把视图刷成当前定义。
    # 老库升级后视图还是旧版（引用了已被删掉的 part.manufacturer），
    # 不刷新的话一查列表就报 no such column。建表语句都是 IF NOT EXISTS，重跑无副作用。
    db.initialize()

    set_version(db, max(version, CURRENT_SCHEMA_VERSION))
    return max(version, CURRENT_SCHEMA_VERSION)
