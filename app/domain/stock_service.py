"""库存批次的读写：入库、出库、盘点、报废。

出库是本系统里唯一"要同时改多行"的操作，必须在一个事务里完成：
    扣减批次数量 + 写流水
任何一步失败都要整体回滚，否则会出现"货扣了但账没记"这种对不上的状态。

出库策略：不指定批次时按 **先进先出**——购买日期早的先出，没填日期的排最后。
这是常识性的默认，能减少批次堆积和过期。
"""

from __future__ import annotations

import logging

from app.domain.models import (
    REASON_COUNT,
    REASON_IN,
    REASON_OUT,
    REASON_SCRAP,
    StockLog,
    StockLot,
)
from app.storage.db import Database

# domain 层记业务动作日志 —— stdlib logging 无 Qt 依赖，不破坏分层铁律。
# 挂在 domain 而不是 UI，是因为托盘快速出库 / 主窗口 / BOM 批量出库
# 三个入口在这里收敛成一处，谁触发都逃不掉。
log = logging.getLogger("edms.stock")


class InsufficientStockError(RuntimeError):
    """库存不足。界面捕获它并给用户一个明确提示，而不是静默扣成负数。"""

    def __init__(self, part_id: int, requested: int, available: int) -> None:
        super().__init__(f"库存不足：需要 {requested}，现有 {available}")
        self.part_id = part_id
        self.requested = requested
        self.available = available


class BulkInsufficientStockError(RuntimeError):
    """批量出库预检不通过。

    带上**全部**不足项，而不是只报第一个 —— 用户要的是"到底缺哪些"，
    一次给全他才好一次性去补货。
    """

    def __init__(self, shortages: list[tuple[int, int, int]]) -> None:
        self.shortages = shortages            # [(part_id, 需求, 现有), ...]
        lines = [f"器件 #{pid}：需要 {need}，现有 {have}"
                 for pid, need, have in shortages]
        super().__init__("库存不足，整批未出库：\n" + "\n".join(lines))


class StockService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ==================================================================
    #  读
    # ==================================================================

    def lots(self, part_id: int) -> list[StockLot]:
        """某器件的全部批次，带位置短标签。数量为 0 的批次排在最后。"""
        rows = self.db.query(
            """
            SELECT l.*, COALESCE(loc.name, '') AS location_label
            FROM stock_lot l
            LEFT JOIN location loc ON loc.id = l.location_id
            WHERE l.part_id = ?
            ORDER BY (l.quantity <= 0),
                     CASE WHEN l.purchase_date = '' THEN 1 ELSE 0 END,
                     l.purchase_date, l.id
            """,
            (part_id,),
        )
        return [StockLot.from_row(r) for r in rows]

    def lot(self, lot_id: int) -> StockLot | None:
        return StockLot.from_row(
            self.db.query_one("SELECT * FROM stock_lot WHERE id = ?", (lot_id,))
        )

    def total_quantity(self, part_id: int) -> int:
        row = self.db.query_one(
            "SELECT COALESCE(SUM(quantity), 0) AS n FROM stock_lot WHERE part_id = ?",
            (part_id,),
        )
        return int(row["n"]) if row else 0

    def quantity_at(self, part_id: int, location_id: int | None) -> int:
        if location_id is None:
            row = self.db.query_one(
                "SELECT COALESCE(SUM(quantity),0) AS n FROM stock_lot "
                "WHERE part_id = ? AND location_id IS NULL",
                (part_id,),
            )
        else:
            row = self.db.query_one(
                "SELECT COALESCE(SUM(quantity),0) AS n FROM stock_lot "
                "WHERE part_id = ? AND location_id = ?",
                (part_id, location_id),
            )
        return int(row["n"]) if row else 0

    def logs(self, part_id: int | None = None, limit: int = 100) -> list[StockLog]:
        if part_id is None:
            rows = self.db.query(
                "SELECT * FROM stock_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.query(
                "SELECT * FROM stock_log WHERE part_id = ? ORDER BY id DESC LIMIT ?",
                (part_id, limit),
            )
        return [StockLog.from_row(r) for r in rows]

    # ==================================================================
    #  入库
    # ==================================================================

    def add_lot(
        self,
        part_id: int,
        quantity: int,
        location_id: int | None = None,
        unit_price: float = 0.0,
        purchase_date: str = "",
        supplier: str = "",
        manufacturer: str = "",
        order_no: str = "",
        batch_no: str = "",
        note: str = "",
        merge_same: bool = True,
    ) -> int:
        """新增一批库存。返回批次 id。

        merge_same=True 时会先找"器件 + 位置 + 单价 + 购买日期 + 厂商 + 供应商"
        完全相同的已有批次，把数量加上去，而不是每次采购都堆一条新记录。

        **厂商也是合并条件的一部分**：YAGEO 的 10kΩ 和厚声的 10kΩ 是同一条
        器件记录下的两批货，价格和来源都不一样，不该被合并成一批。
        """
        if quantity <= 0:
            raise ValueError("入库数量必须大于 0")

        with self.db.transaction() as conn:
            lot_id: int | None = None

            if merge_same:
                row = conn.execute(
                    """
                    SELECT id FROM stock_lot
                    WHERE part_id = ? AND quantity > 0
                      AND IFNULL(location_id, -1) = IFNULL(?, -1)
                      AND ABS(unit_price - ?) < 1e-9
                      AND purchase_date = ?
                      AND manufacturer = ? AND supplier = ?
                    ORDER BY id LIMIT 1
                    """,
                    (part_id, location_id, float(unit_price), purchase_date,
                     manufacturer, supplier),
                ).fetchone()
                if row is not None:
                    lot_id = row["id"]
                    conn.execute(
                        "UPDATE stock_lot SET quantity = quantity + ?, "
                        "updated_at = datetime('now','localtime') WHERE id = ?",
                        (quantity, lot_id),
                    )

            if lot_id is None:
                cur = conn.execute(
                    """
                    INSERT INTO stock_lot(part_id, location_id, quantity, unit_price,
                                          purchase_date, supplier, manufacturer,
                                          order_no, batch_no, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (part_id, location_id, quantity, float(unit_price), purchase_date,
                     supplier, manufacturer, order_no, batch_no, note),
                )
                lot_id = cur.lastrowid

            conn.execute(
                "INSERT INTO stock_log(part_id, lot_id, delta, reason, ref) VALUES (?, ?, ?, ?, ?)",
                (part_id, lot_id, quantity, REASON_IN, note or supplier),
            )

        log.info("入库：器件#%s 数量 %d 单价 %s 位置 %s 批次#%s%s",
                 part_id, quantity, unit_price, location_id, lot_id,
                 "（并入原批次）" if merge_same else "")

        return lot_id

    # ==================================================================
    #  出库
    # ==================================================================

    def withdraw(
        self,
        part_id: int,
        quantity: int,
        lot_id: int | None = None,
        ref: str = "",
        reason: str = REASON_OUT,
    ) -> list[tuple[int, int]]:
        """出库。返回 [(批次id, 取出数量), ...]。

        lot_id 指定了就只从那一批扣；不指定则按先进先出跨批次扣。
        库存不够会抛 InsufficientStockError，**不会**把数量扣成负数。
        """
        if quantity <= 0:
            raise ValueError("出库数量必须大于 0")
        with self.db.transaction() as conn:
            taken = self._withdraw_within(conn, part_id, quantity, lot_id, ref, reason)
        log.info("出库：器件#%s 数量 %d 来源 %s%s",
                 part_id, quantity,
                 [(lot, n) for lot, n in taken],
                 f" 用途 {ref}" if ref else "")
        return taken

    def _withdraw_within(
        self,
        conn,
        part_id: int,
        quantity: int,
        lot_id: int | None,
        ref: str,
        reason: str,
    ) -> list[tuple[int, int]]:
        """出库的实际动作，**不开事务** —— 由调用方决定事务边界。

        单独抽出来的原因是批量出库要把几十行扣减放进同一个事务里；
        如果这里再自己 BEGIN 一次，嵌套事务会直接报错。
        """
        if lot_id is not None:
            rows = conn.execute(
                "SELECT id, quantity FROM stock_lot WHERE id = ? AND part_id = ?",
                (lot_id, part_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, quantity FROM stock_lot
                WHERE part_id = ? AND quantity > 0
                ORDER BY CASE WHEN purchase_date = '' THEN 1 ELSE 0 END,
                         purchase_date, id
                """,
                (part_id,),
            ).fetchall()

        available = sum(int(r["quantity"]) for r in rows)
        if available < quantity:
            raise InsufficientStockError(part_id, quantity, available)

        taken: list[tuple[int, int]] = []
        remaining = quantity
        for r in rows:
            if remaining <= 0:
                break
            have = int(r["quantity"])
            if have <= 0:
                continue
            use = min(have, remaining)

            conn.execute(
                "UPDATE stock_lot SET quantity = quantity - ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (use, r["id"]),
            )
            conn.execute(
                "INSERT INTO stock_log(part_id, lot_id, delta, reason, ref) "
                "VALUES (?, ?, ?, ?, ?)",
                (part_id, r["id"], -use, reason, ref),
            )
            taken.append((int(r["id"]), use))
            remaining -= use

        if remaining > 0:
            # 理论上到不了这里（前面已校验过 available），保险起见
            raise InsufficientStockError(part_id, quantity, available)

        return taken

    def preview_withdraw_cost(
        self, part_id: int, quantity: int, lot_id: int | None = None
    ) -> list[tuple[int, int, float]]:
        """**只算不动**：模拟一次出库会从哪些批次扣、各扣多少、单价多少。

        返回 `[(批次id, 扣掉的数量, 该批次单价), ...]`。

        **排序必须和 `_withdraw_within` 完全一致** —— 预览用的排序跟实际扣减
        不一样，界面上算出来的钱就是假的。改这两处的 ORDER BY 必须一起改。

        库存不够时只返回够得着的部分，缺多少由调用方自己显示
        （出库面板本来就有"现有库存 / 需求"那一列在管这件事）。
        """
        if lot_id is not None:
            rows = self.db.query(
                "SELECT id, quantity, unit_price FROM stock_lot "
                "WHERE id = ? AND part_id = ?",
                (lot_id, part_id),
            )
        else:
            rows = self.db.query(
                """
                SELECT id, quantity, unit_price FROM stock_lot
                WHERE part_id = ? AND quantity > 0
                ORDER BY CASE WHEN purchase_date = '' THEN 1 ELSE 0 END,
                         purchase_date, id
                """,
                (part_id,),
            )

        taken: list[tuple[int, int, float]] = []
        remaining = quantity
        for r in rows:
            if remaining <= 0:
                break
            have = int(r["quantity"])
            if have <= 0:
                continue
            use = min(have, remaining)
            taken.append((int(r["id"]), use, float(r["unit_price"])))
            remaining -= use
        return taken

    def withdraw_many(
        self,
        requests: list[tuple[int, int, str] | tuple[int, int, str, int | None]],
    ) -> list[tuple[int, int, list[tuple[int, int]]]]:
        """批量出库。

        每项可以是 3 元组 `(器件id, 数量, 备注)`，也可以是 4 元组
        `(器件id, 数量, 备注, 批次id或None)` —— 后者用于"这次就从那一盘整卷里扣"。

        **全有才出**：任何一项库存不足，整批取消，一颗都不扣。
        出半套比不出更麻烦 —— 你会搞不清板子上到底装了什么。

        整个过程在**一个事务**里，所以不会出现"扣了前 20 行、第 21 行失败"
        这种中途状态。
        """
        if not requests:
            return []

        # 归一化成 4 元组，同时汇总需求。
        # 必须**按器件汇总**：同一个器件可能出现在多行，逐行看都够、
        # 加起来不够，这种情况最容易出事。
        normalized: list[tuple[int, int, str, int | None]] = []
        need: dict[int, int] = {}
        for req in requests:
            part_id, qty, ref = req[0], req[1], req[2]
            lot_id = req[3] if len(req) > 3 else None
            if qty <= 0:
                raise ValueError(f"出库数量必须大于 0（器件 #{part_id}）")
            normalized.append((part_id, qty, ref, lot_id))
            need[part_id] = need.get(part_id, 0) + qty

        shortages: list[tuple[int, int, int]] = []
        for part_id, total in need.items():
            have = self.total_quantity(part_id)
            if have < total:
                shortages.append((part_id, total, have))

        # 指定了批次的还要单独核那一批 ——
        # 这个器件总量够，不代表你指定的那一批够。
        if not shortages:
            per_lot: dict[int, int] = {}
            for _pid, qty, _ref, lot_id in normalized:
                if lot_id is not None:
                    per_lot[lot_id] = per_lot.get(lot_id, 0) + qty
            for lot_id, total in per_lot.items():
                lot = self.lot(lot_id)
                have = lot.quantity if lot else 0
                if have < total:
                    shortages.append((lot.part_id if lot else -1, total, have))

        if shortages:
            raise BulkInsufficientStockError(shortages)

        results: list[tuple[int, int, list[tuple[int, int]]]] = []
        with self.db.transaction() as conn:
            for part_id, qty, ref, lot_id in normalized:
                taken = self._withdraw_within(conn, part_id, qty, lot_id, ref, REASON_OUT)
                results.append((part_id, qty, taken))
        log.info("批量出库：%d 行，合计 %d 颗，来源 %s",
                 len(results), sum(q for _, q, _ in results),
                 [(pid, qty, [(lot, n) for lot, n in taken])
                  for pid, qty, taken in results])
        return results

    # ==================================================================
    #  盘点 / 报废 / 修改
    # ==================================================================

    def adjust(self, lot_id: int, new_quantity: int, ref: str = "") -> int:
        """盘点：把批次数量直接改成实测值，并记一条差额流水。返回差额。"""
        if new_quantity < 0:
            raise ValueError("数量不能为负")

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT part_id, quantity FROM stock_lot WHERE id = ?", (lot_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"批次不存在：{lot_id}")

            delta = new_quantity - int(row["quantity"])
            if delta == 0:
                return 0

            conn.execute(
                "UPDATE stock_lot SET quantity = ?, updated_at = datetime('now','localtime') "
                "WHERE id = ?",
                (new_quantity, lot_id),
            )
            conn.execute(
                "INSERT INTO stock_log(part_id, lot_id, delta, reason, ref) VALUES (?, ?, ?, ?, ?)",
                (row["part_id"], lot_id, delta, REASON_COUNT, ref),
            )
        log.info("盘点：批次#%s 器件#%s %d -> %d（差额 %+d）",
                 lot_id, row["part_id"], int(row["quantity"]), new_quantity, delta)
        return delta

    def scrap(self, lot_id: int, quantity: int, ref: str = "") -> None:
        """报废：从指定批次扣掉一批坏货。"""
        lot = self.lot(lot_id)
        if lot is None:
            raise ValueError(f"批次不存在：{lot_id}")
        log.info("报废：批次#%s 器件#%s 数量 %d", lot_id, lot.part_id, quantity)
        self.withdraw(lot.part_id, quantity, lot_id=lot_id, ref=ref, reason=REASON_SCRAP)

    def update_lot(self, lot_id: int, **fields) -> None:
        """修改批次的元信息（位置、单价、日期、供应商等）。数量请走 adjust()。"""
        allowed = {
            "location_id", "unit_price", "currency", "purchase_date",
            "supplier", "manufacturer", "order_no", "batch_no", "note",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return

        assignments = ", ".join(f"{k} = ?" for k in sets)
        params = list(sets.values()) + [lot_id]
        self.db.execute(
            f"UPDATE stock_lot SET {assignments}, updated_at = datetime('now','localtime') "
            f"WHERE id = ?",
            params,
        )
        log.info("改批次信息：批次#%s %s", lot_id, sets)

    def delete_lot(self, lot_id: int) -> None:
        """删除批次。流水会保留但 lot_id 置空（历史不该被抹掉）。"""
        lot = self.lot(lot_id)
        if lot is not None:
            log.info("删除批次：批次#%s 器件#%s（原 %d 颗 @ %s）",
                     lot_id, lot.part_id, lot.quantity, lot.unit_price)
        self.db.execute("DELETE FROM stock_lot WHERE id = ?", (lot_id,))

    def set_unit_price(self, part_id: int, unit_price: float) -> int:
        """把某个器件**还有货的所有批次**的单价统一设为 `unit_price`，返回影响的批次数。

        用于事后补价：入库时忘了填单价（存成 0），事后在 BOM 成本明细里补上。
        写的是批次（价格本来就在批次上），不是器件。

        **刻意不写 stock_log** —— 改单价不是出入库，数量没动。往流水里塞一条
        "改价"会污染"库存可重建"这条性质（流水重算出来的是数量，不是价格）。
        历史上它因此没有审计痕迹；现在有日志了，审计去 log/ 目录看。
        """
        if unit_price < 0:
            raise ValueError("单价不能是负数")

        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE stock_lot SET unit_price = ?, "
                "updated_at = datetime('now','localtime') "
                "WHERE part_id = ? AND quantity > 0",
                (float(unit_price), part_id),
            )
            touched = cur.rowcount
        log.info("改价：器件#%s 所有在库批次 -> %s，涉及 %d 个批次",
                 part_id, unit_price, touched)
        return touched

    # ==================================================================
    #  统计
    # ==================================================================

    def inventory_value(self) -> float:
        """库存总货值（按各批次单价加权）。"""
        row = self.db.query_one(
            "SELECT COALESCE(SUM(quantity * unit_price), 0) AS v FROM stock_lot WHERE quantity > 0"
        )
        return float(row["v"]) if row else 0.0

    def average_price(self, part_id: int) -> float:
        """该器件在库批次的加权均价（和 `v_part_overview.avg_price` 同一公式）。

        给入库面板做"价格异常提醒"用的 —— 得先知道现在的均价是多少，
        才能判断用户填的这个价是不是手滑多敲了一个零。
        """
        row = self.db.query_one(
            "SELECT CASE WHEN COALESCE(SUM(quantity), 0) > 0 "
            "THEN SUM(quantity * unit_price) / SUM(quantity) ELSE 0 END AS p "
            "FROM stock_lot WHERE part_id = ? AND quantity > 0",
            (part_id,),
        )
        return float(row["p"]) if row else 0.0
