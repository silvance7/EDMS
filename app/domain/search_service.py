"""检索：把 SearchQuery 翻译成一条 SQL。

两个使用场景共用这一个入口：
    主界面      —— 分类树 + 关键词 + 封装/位置/低库存等筛选条件
    快速查询浮窗 —— 只有关键词，要求尽快出结果

关键词按空白切分，多个词之间是 **AND**（每个词都必须出现在 search_text 里）。
所以 "10k 0603" 会同时要求两个词命中，符合直觉而不是把结果搅成一锅粥。

所有值都走参数绑定；排序字段走白名单映射，不拼接用户输入。
"""

from __future__ import annotations

from app.domain.models import PartOverview, SearchQuery
from app.domain.tree_service import TreeService
from app.storage.db import Database

# 排序字段白名单：键是界面能传的值，值是真正的 SQL 片段。
_ORDER_COLUMNS = {
    "name": "v.name COLLATE NOCASE",
    "total_qty": "v.total_qty",
    "updated_at": "v.updated_at",
    "last_purchase": "v.last_purchase",
    "avg_price": "v.avg_price",
    "footprint": "v.footprint COLLATE NOCASE",
}


class SearchService:
    def __init__(self, db: Database, tree: TreeService | None = None) -> None:
        self.db = db
        self.tree = tree or TreeService(db)

    # ==================================================================
    #  对外
    # ==================================================================

    def search(self, q: SearchQuery) -> list[PartOverview]:
        sql, params = self._build(q)
        rows = self.db.query(sql, params)
        results = [PartOverview.from_row(r) for r in rows]
        self._attach_display_fields(results)
        return results

    def count(self, q: SearchQuery) -> int:
        """只数数量，不取数据。给状态栏显示"共 N 条"用。"""
        where, params = self._build_where(q)
        row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM v_part_overview v WHERE {where}", params
        )
        return int(row["n"]) if row else 0

    def footprints(self) -> list[str]:
        """库里实际用过的封装清单，供下拉筛选。"""
        rows = self.db.query(
            "SELECT DISTINCT footprint FROM part WHERE footprint <> '' "
            "ORDER BY footprint COLLATE NOCASE"
        )
        return [r["footprint"] for r in rows]

    def low_stock(self, limit: int = 100) -> list[PartOverview]:
        return self.search(SearchQuery(only_low_stock=True, limit=limit))

    def quick(self, keyword: str, limit: int = 20) -> list[PartOverview]:
        """快速查询浮窗用的入口：只按关键词，取少量结果。"""
        return self.search(SearchQuery(keyword=keyword, limit=limit))

    # ==================================================================
    #  内部
    # ==================================================================

    def _build(self, q: SearchQuery) -> tuple[str, list]:
        where, params = self._build_where(q)

        order_col = _ORDER_COLUMNS.get(q.order_by, _ORDER_COLUMNS["name"])
        direction = "DESC" if q.order_desc else "ASC"

        # 没有关键词时按"缺货优先"排，让该补货的一眼看到
        secondary = ", v.name COLLATE NOCASE ASC"
        if not q.tokens and q.order_by == "name":
            order_clause = (
                "CASE WHEN v.total_qty <= 0 THEN 0 "
                "WHEN v.min_stock > 0 AND v.total_qty <= v.min_stock THEN 1 "
                "ELSE 2 END ASC, v.name COLLATE NOCASE ASC"
            )
        else:
            order_clause = f"{order_col} {direction}{secondary}"

        sql = (
            f"SELECT v.* FROM v_part_overview v WHERE {where} "
            f"ORDER BY {order_clause} LIMIT ?"
        )
        params = list(params) + [max(int(q.limit), 1)]
        return sql, params

    def _build_where(self, q: SearchQuery) -> tuple[str, list]:
        clauses: list[str] = ["1 = 1"]
        params: list = []

        if q.only_active:
            clauses.append("v.is_active = 1")

        # 分类（含所有子孙分类）
        if q.category_id is not None:
            cat_ids = self.tree.category_ids_with_children(q.category_id)
            if cat_ids:
                clauses.append(f"v.category_id IN ({','.join('?' * len(cat_ids))})")
                params.extend(cat_ids)

        # 「未分类」筛选（主窗口树上的虚拟节点）。必须是独立开关：
        # 想靠 category_id=-1 走上面的分支的话，查无此分类 → 空列表 → 不加条件
        # → 返回全部器件，看起来像"筛选坏了"。
        if q.uncategorized:
            clauses.append("v.category_id IS NULL")

        if q.footprint:
            clauses.append("v.footprint = ?")
            params.append(q.footprint)

        # 关键词：每个词一个 LIKE，AND 关系
        for token in q.tokens:
            clauses.append("v.search_text LIKE ?")
            params.append(f"%{token}%")

        # 位置（含所有子孙位置），只算还有货的批次
        if q.location_id is not None:
            loc_ids = self.tree.location_ids_with_children(q.location_id)
            if loc_ids:
                placeholders = ",".join("?" * len(loc_ids))
                clauses.append(
                    "EXISTS (SELECT 1 FROM stock_lot sl "
                    "        WHERE sl.part_id = v.id AND sl.quantity > 0 "
                    f"       AND sl.location_id IN ({placeholders}))"
                )
                params.extend(loc_ids)

        # 参数范围筛选："阻值 10k ~ 100k"
        for name, low, high in q.param_ranges:
            sub = ["t.name = ?", "pp.part_id = v.id"]
            sub_params: list = [name]
            if low is not None:
                sub.append("pp.value_num >= ?")
                sub_params.append(float(low))
            if high is not None:
                sub.append("pp.value_num <= ?")
                sub_params.append(float(high))
            clauses.append(
                "EXISTS (SELECT 1 FROM part_param pp "
                "        JOIN param_template t ON t.id = pp.template_id "
                f"       WHERE {' AND '.join(sub)})"
            )
            params.extend(sub_params)

        if q.only_low_stock:
            # min_stock = 0 视为没设阈值，不算低库存
            clauses.append("v.min_stock > 0 AND v.total_qty <= v.min_stock")

        return " AND ".join(clauses), params

    def _attach_display_fields(self, results: list[PartOverview]) -> None:
        """补齐界面要显示、但视图里没有的字段。

        全部用两次批量查询搞定，绝不在循环里逐行查库——
        那是把 N 次查询变成 N+1 次的最经典写法，列表一长就废。
        """
        if not results:
            return

        part_ids = [r.id for r in results]
        placeholders = ",".join("?" * len(part_ids))

        # --- 分类路径 ---
        cat_rows = self.db.query("SELECT id, name, parent_id FROM category")
        cat_by_id = {r["id"]: (r["name"], r["parent_id"]) for r in cat_rows}

        def cat_path(cat_id: int | None) -> str:
            names: list[str] = []
            cur = cat_id
            for _ in range(32):
                if cur is None or cur not in cat_by_id:
                    break
                name, parent = cat_by_id[cur]
                names.append(name)
                cur = parent
            return " / ".join(reversed(names))

        # --- 各位置的库存分布："A1×100 · B2×40" ---
        loc_rows = self.db.query(
            f"""
            SELECT sl.part_id,
                   COALESCE(NULLIF(loc.code, ''), loc.name, '未指定') AS label,
                   SUM(sl.quantity) AS qty
            FROM stock_lot sl
            LEFT JOIN location loc ON loc.id = sl.location_id
            WHERE sl.part_id IN ({placeholders}) AND sl.quantity > 0
            GROUP BY sl.part_id, sl.location_id
            ORDER BY sl.part_id, qty DESC
            """,
            part_ids,
        )
        loc_map: dict[int, list[str]] = {}
        for r in loc_rows:
            loc_map.setdefault(r["part_id"], []).append(f"{r['label']}×{r['qty']}")

        for item in results:
            item.category_path = cat_path(item.category_id)
            item.location_summary = " · ".join(loc_map.get(item.id, [])) or "—"
