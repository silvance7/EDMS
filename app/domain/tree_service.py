"""分类树与存放位置树的读写。

两棵树结构完全一样，所以逻辑写在一起，只是表名不同。
关键点是**递归子查询**：筛选"电阻"分类时要连带它的所有子孙分类，
数据库层面用 recursive CTE 一次算出来，比在 Python 里递归查库快得多。
"""

from __future__ import annotations

from app.domain.models import Category, Location
from app.storage.db import Database


class TreeService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    #  分类
    # ==================================================================

    def list_categories(self) -> list[Category]:
        rows = self.db.query(
            "SELECT * FROM category ORDER BY sort_order, name COLLATE NOCASE"
        )
        return [Category.from_row(r) for r in rows]

    def category_children(self, parent_id: int | None) -> list[Category]:
        if parent_id is None:
            rows = self.db.query(
                "SELECT * FROM category WHERE parent_id IS NULL "
                "ORDER BY sort_order, name COLLATE NOCASE"
            )
        else:
            rows = self.db.query(
                "SELECT * FROM category WHERE parent_id = ? "
                "ORDER BY sort_order, name COLLATE NOCASE",
                (parent_id,),
            )
        return [Category.from_row(r) for r in rows]

    def category_path(self, category_id: int | None, sep: str = " / ") -> str:
        """从根到该分类的完整路径，例如 "电阻 / 贴片电阻"。"""
        names: list[str] = []
        cur = category_id
        # 加个循环上限，万一数据被手工改出环也不会死循环
        for _ in range(32):
            if cur is None:
                break
            row = self.db.query_one("SELECT name, parent_id FROM category WHERE id = ?", (cur,))
            if row is None:
                break
            names.append(row["name"])
            cur = row["parent_id"]
        return sep.join(reversed(names))

    def category_ids_with_children(self, category_id: int | None) -> list[int]:
        """该分类及其全部后代分类的 id。用于"选中电阻，列出所有电阻"。"""
        if category_id is None:
            return []
        rows = self.db.query(
            """
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM category WHERE id = ?
                UNION ALL
                SELECT c.id FROM category c JOIN sub ON c.parent_id = sub.id
            )
            SELECT id FROM sub
            """,
            (category_id,),
        )
        return [r["id"] for r in rows]

    def create_category(self, name: str, parent_id: int | None = None) -> int:
        name = name.strip()
        if not name:
            raise ValueError("分类名不能为空")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO category(name, parent_id) VALUES (?, ?)", (name, parent_id)
            )
            return cur.lastrowid

    def rename_category(self, category_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("分类名不能为空")
        self.db.execute("UPDATE category SET name = ? WHERE id = ?", (new_name, category_id))

    def delete_category(self, category_id: int) -> None:
        """删除分类。子分类级联删除；引用它的器件 category_id 置空（不删器件）。"""
        self.db.execute("DELETE FROM category WHERE id = ?", (category_id,))

    # ==================================================================
    #  存放位置
    # ==================================================================

    def list_locations(self) -> list[Location]:
        rows = self.db.query(
            "SELECT * FROM location ORDER BY sort_order, name COLLATE NOCASE"
        )
        return [Location.from_row(r) for r in rows]

    def location_children(self, parent_id: int | None) -> list[Location]:
        if parent_id is None:
            rows = self.db.query(
                "SELECT * FROM location WHERE parent_id IS NULL "
                "ORDER BY sort_order, name COLLATE NOCASE"
            )
        else:
            rows = self.db.query(
                "SELECT * FROM location WHERE parent_id = ? "
                "ORDER BY sort_order, name COLLATE NOCASE",
                (parent_id,),
            )
        return [Location.from_row(r) for r in rows]

    def location_path(self, location_id: int | None, sep: str = " / ") -> str:
        names: list[str] = []
        cur = location_id
        for _ in range(32):
            if cur is None:
                break
            row = self.db.query_one("SELECT name, parent_id FROM location WHERE id = ?", (cur,))
            if row is None:
                break
            names.append(row["name"])
            cur = row["parent_id"]
        return sep.join(reversed(names))

    def location_short(self, location_id: int | None) -> str:
        """表格里显示的位置名。就用名字 —— 用户明确说过位置不要编号。"""
        if location_id is None:
            return "—"
        row = self.db.query_one("SELECT name FROM location WHERE id = ?", (location_id,))
        if row is None:
            return "—"
        return row["name"]

    def location_ids_with_children(self, location_id: int | None) -> list[int]:
        if location_id is None:
            return []
        rows = self.db.query(
            """
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM location WHERE id = ?
                UNION ALL
                SELECT l.id FROM location l JOIN sub ON l.parent_id = sub.id
            )
            SELECT id FROM sub
            """,
            (location_id,),
        )
        return [r["id"] for r in rows]

    def create_location(self, name: str, parent_id: int | None = None, code: str = "") -> int:
        name = name.strip()
        if not name:
            raise ValueError("位置名不能为空")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO location(name, code, parent_id) VALUES (?, ?, ?)",
                (name, code.strip(), parent_id),
            )
            return cur.lastrowid

    def rename_location(self, location_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("位置名不能为空")
        self.db.execute("UPDATE location SET name = ? WHERE id = ?", (new_name, location_id))

    def update_location(self, location_id: int, name: str | None = None,
                        code: str | None = None) -> None:
        """改位置的名字 / 短码。传 None 表示那一项不动。

        短码（code）是贴在抽屉上的标签，表格里显示"A1"靠它、将来打印二维码也靠它，
        所以要能**单独**改 —— 只有 rename 的话编号就改不了。
        传空串表示清掉编号；重码由库里的唯一索引管着。
        """
        sets: list[str] = []
        params: list = []
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("位置名不能为空")
            sets.append("name = ?")
            params.append(name)
        if code is not None:
            sets.append("code = ?")
            params.append(code.strip())
        if not sets:
            return
        params.append(location_id)
        self.db.execute(
            f"UPDATE location SET {', '.join(sets)} WHERE id = ?", params
        )

    def delete_location(self, location_id: int) -> None:
        """删除位置。子位置级联删除；该位置上的库存批次 location_id 置空。"""
        self.db.execute("DELETE FROM location WHERE id = ?", (location_id,))

    def location_label_map(self) -> dict[int, str]:
        """一次性取出所有 id -> 显示标签，避免列表页逐行查库。"""
        rows = self.db.query("SELECT id, name FROM location")
        return {r["id"]: r["name"] for r in rows}
