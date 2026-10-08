"""器件（物料主数据）的读写。

本模块负责维护 `part.search_text` 这个冗余检索列。它是冗余的，
所以**任何**改动器件本身或其参数值的入口，最后都必须调用 _refresh_search_text()，
否则检索结果会和实际数据脱节。

engineering_forms() 是个关键的小工具：用户习惯说"10k"，而库里存的是 10000 Ω。
把数值额外展开成带 SI 前缀的写法一起塞进检索列，"10k" 和 "10K" 都能搜到。
"""

from __future__ import annotations

import logging

from app.domain.models import Part, PartParamView, ParamTemplate
from app.domain.tree_service import TreeService
from app.storage.db import Database

log = logging.getLogger("edms.part")

# (倍率, SI 前缀)。用于把 10000 展开成 10k 这种人类写法。
_SI_PREFIXES: list[tuple[float, str]] = [
    (1e12, "T"),
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1e-3, "m"),
    (1e-6, "u"),
    (1e-9, "n"),
    (1e-12, "p"),
]


def engineering_forms(value: float | None, unit: str = "") -> list[str]:
    """把数值展开成几种常见写法，全部塞进检索列。

    >>> engineering_forms(10000, "Ω")
    ['10000', '10k', '10kΩ']
    >>> engineering_forms(0.1, "uF")
    ['0.1', '100m', '100muF']
    """
    if value is None:
        return []

    forms: list[str] = [f"{value:g}"]

    for scale, prefix in _SI_PREFIXES:
        if abs(value) < scale:
            continue
        scaled = value / scale
        # 只保留看起来"整"的写法，避免 12345 -> 12.345k 这种噪音
        if abs(scaled - round(scaled)) > 1e-9:
            break
        whole = int(round(scaled))
        forms.append(f"{whole}{prefix}")
        if unit:
            forms.append(f"{whole}{prefix}{unit}")
        # 同时给一份小写前缀，用户可能敲 10K / 100NF
        forms.append(f"{whole}{prefix.lower()}")
        if unit:
            forms.append(f"{whole}{prefix.lower()}{unit.lower()}")
        break

    return forms


class PartService:
    def __init__(self, db: Database, tree: TreeService | None = None) -> None:
        self.db = db
        self.tree = tree or TreeService(db)

    # ==================================================================
    #  读
    # ==================================================================

    def get(self, part_id: int) -> Part | None:
        return Part.from_row(self.db.query_one("SELECT * FROM part WHERE id = ?", (part_id,)))

    def get_params(self, part_id: int) -> list[PartParamView]:
        rows = self.db.query(
            """
            SELECT t.id AS template_id, t.name, t.unit, t.data_type, t.sort_order,
                   pp.value_num, pp.value_text
            FROM part_param pp
            JOIN param_template t ON t.id = pp.template_id
            WHERE pp.part_id = ?
            ORDER BY t.sort_order, t.name
            """,
            (part_id,),
        )
        return [
            PartParamView(
                template_id=r["template_id"],
                name=r["name"],
                unit=r["unit"],
                data_type=r["data_type"],
                value_num=r["value_num"],
                value_text=r["value_text"],
                sort_order=r["sort_order"],
            )
            for r in rows
        ]

    def templates_for_category(self, category_id: int | None) -> list[ParamTemplate]:
        """该分类可用的参数模板 —— 含**从祖先分类继承**来的。

        这是 Part-DB 那套设计的落地：高层分类定义公共字段，子分类追加特有字段。
        同名参数就近覆盖（子分类赢）。
        """
        if category_id is None:
            return []

        # 从当前分类往上走，近的在前
        chain = self._ancestor_ids(category_id)
        if not chain:
            return []

        placeholders = ",".join("?" * len(chain))
        rows = self.db.query(
            f"SELECT * FROM param_template WHERE category_id IN ({placeholders})",
            chain,
        )
        by_id = {r["id"]: ParamTemplate.from_row(r) for r in rows}

        # 按"离当前分类由近到远"排序，同名的保留最先出现的那个
        seen: dict[str, ParamTemplate] = {}
        for cat_id in chain:
            for tpl in sorted(
                (t for t in by_id.values() if t.category_id == cat_id),
                key=lambda t: (t.sort_order, t.name),
            ):
                seen.setdefault(tpl.name, tpl)

        return sorted(seen.values(), key=lambda t: (t.sort_order, t.name))

    def _ancestor_ids(self, category_id: int) -> list[int]:
        """[自己, 父, 祖父, ..., 根]"""
        rows = self.db.query(
            """
            WITH RECURSIVE up(id, parent_id, depth) AS (
                SELECT id, parent_id, 0 FROM category WHERE id = ?
                UNION ALL
                SELECT c.id, c.parent_id, up.depth + 1
                FROM category c JOIN up ON c.id = up.parent_id
            )
            SELECT id FROM up ORDER BY depth
            """,
            (category_id,),
        )
        return [r["id"] for r in rows]

    # ==================================================================
    #  写
    # ==================================================================

    def create(
        self,
        part: Part,
        params: dict[int, float | str | None] | None = None,
    ) -> int:
        """新建器件。params 是 {模板id: 取值}。返回新器件 id。"""
        name = part.name.strip()
        if not name:
            raise ValueError("器件名称不能为空")
        part.name = name

        with self.db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO part(name, category_id, mpn, footprint,
                                 description, datasheet, keywords, min_stock, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    part.name,
                    part.category_id,
                    part.mpn.strip(),
                    part.footprint.strip(),
                    part.description.strip(),
                    part.datasheet.strip(),
                    part.keywords.strip(),
                    int(part.min_stock or 0),
                    1 if part.is_active else 0,
                ),
            )
            part_id = cur.lastrowid
            self._write_params(conn, part_id, params or {})

        self._refresh_search_text(part_id)
        log.info("新增器件：#%d %s（封装 %s，分类 %s）",
                 part_id, part.name, part.footprint or "—", part.category_id or "未分类")
        return part_id

    def update(
        self,
        part: Part,
        params: dict[int, float | str | None] | None = None,
    ) -> None:
        if part.id is None:
            raise ValueError("更新器件时必须带 id")
        name = part.name.strip()
        if not name:
            raise ValueError("器件名称不能为空")

        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE part SET name = ?, category_id = ?, mpn = ?,
                                footprint = ?, description = ?, datasheet = ?, keywords = ?,
                                min_stock = ?, is_active = ?,
                                updated_at = datetime('now','localtime')
                WHERE id = ?
                """,
                (
                    name,
                    part.category_id,
                    part.mpn.strip(),
                    part.footprint.strip(),
                    part.description.strip(),
                    part.datasheet.strip(),
                    part.keywords.strip(),
                    int(part.min_stock or 0),
                    1 if part.is_active else 0,
                    part.id,
                ),
            )
            if params is not None:
                self._write_params(conn, part.id, params)

        self._refresh_search_text(part.id)
        log.info("修改器件：#%d %s", part.id, part.name)

    def set_active(self, part_id: int, active: bool) -> None:
        self.db.execute(
            "UPDATE part SET is_active = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (1 if active else 0, part_id),
        )
        log.info("器件 #%d 状态 -> %s", part_id, "启用" if active else "停用")

    def delete(self, part_id: int) -> None:
        """删除器件。它的参数、库存批次、流水都会被级联删掉（见 schema 的 ON DELETE CASCADE）。"""
        part = self.get(part_id)
        if part is not None:
            log.warning("删除器件：#%d %s（其参数/批次/流水已级联删除）",
                        part_id, part.name)
        self.db.execute("DELETE FROM part WHERE id = ?", (part_id,))

    def add_template(self, category_id: int | None, name: str, unit: str = "", data_type: str = "num") -> int:
        """给某分类新增一个参数模板。"""
        name = name.strip()
        if not name:
            raise ValueError("参数名不能为空")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO param_template(category_id, name, unit, data_type) VALUES (?, ?, ?, ?)",
                (category_id, name, unit.strip(), data_type),
            )
            return cur.lastrowid

    def templates_of_category(self, category_id: int) -> list[ParamTemplate]:
        """**本级自己定义**的参数模板（不含从祖先继承来的）。

        删除操作只能用这个：继承来的参数属于上级分类，定义在那儿，
        要删也得去那个分类上删 —— 否则会出现"子分类删掉了爹的参数"。
        """
        rows = self.db.query(
            "SELECT * FROM param_template WHERE category_id = ? "
            "ORDER BY sort_order, name COLLATE NOCASE",
            (category_id,),
        )
        return [ParamTemplate.from_row(r) for r in rows]

    def template_impact(self, template_id: int) -> tuple[int, int]:
        """删掉这个参数会连带清掉多少东西：`(器件数, 参数值行数)`。

        给确认框用 —— 删参数是不可逆的，界面上必须先说清代价。
        """
        row = self.db.query_one(
            "SELECT COUNT(DISTINCT part_id) AS parts, COUNT(*) AS n_values "
            "FROM part_param WHERE template_id = ?",
            (template_id,),
        )
        return (int(row["parts"]), int(row["n_values"])) if row else (0, 0)

    def delete_template(self, template_id: int) -> None:
        """删除参数模板。

        该分类下所有器件已填的对应取值，由 schema 的
        `part_param.template_id ... ON DELETE CASCADE` 连带清掉 ——
        留下的孤儿值没人显示、进不了参数筛选，纯垃圾。

        **调用方负责接着 `rebuild_all_search_text()`**：参数值进过检索列，
        删完不重建，`search_text` 里就留着再也搜不到、也删不掉的死词。
        """
        row = self.db.query_one(
            "SELECT t.name, t.unit, c.name AS cat FROM param_template t "
            "LEFT JOIN category c ON c.id = t.category_id WHERE t.id = ?",
            (template_id,),
        )
        if row is None:
            raise ValueError("该参数不存在（可能已经被删掉了）")
        self.db.execute("DELETE FROM param_template WHERE id = ?", (template_id,))
        log.warning("删除分类参数「%s」（分类 %s，单位 %s）—— 各器件已填的取值一并清空",
                    row["name"], row["cat"] or "—", row["unit"] or "—")

    # ==================================================================
    #  内部
    # ==================================================================

    @staticmethod
    def _write_params(conn, part_id: int, params: dict[int, float | str | None]) -> None:
        """覆盖式写入参数。空值等于删除该参数。"""
        for template_id, value in params.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                conn.execute(
                    "DELETE FROM part_param WHERE part_id = ? AND template_id = ?",
                    (part_id, template_id),
                )
                continue

            if isinstance(value, str):
                conn.execute(
                    """
                    INSERT INTO part_param(part_id, template_id, value_num, value_text)
                    VALUES (?, ?, NULL, ?)
                    ON CONFLICT(part_id, template_id) DO UPDATE
                        SET value_num = NULL, value_text = excluded.value_text
                    """,
                    (part_id, template_id, value.strip()),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO part_param(part_id, template_id, value_num, value_text)
                    VALUES (?, ?, ?, '')
                    ON CONFLICT(part_id, template_id) DO UPDATE
                        SET value_num = excluded.value_num, value_text = ''
                    """,
                    (part_id, template_id, float(value)),
                )

    def _refresh_search_text(self, part_id: int) -> None:
        """重建检索列：器件自身字段 + 分类路径 + 参数值（含 SI 前缀展开）。"""
        row = self.db.query_one("SELECT * FROM part WHERE id = ?", (part_id,))
        if row is None:
            return

        chunks: list[str] = [
            row["name"],
            row["mpn"],
            row["footprint"],
            row["description"],
            row["keywords"],
        ]

        # 厂商和供应商记在**批次**上，但用户会拿厂牌名来找料（"我那盘 YAGEO 的"），
        # 所以也塞进检索列。
        for lot in self.db.query(
            "SELECT DISTINCT manufacturer, supplier FROM stock_lot WHERE part_id = ?",
            (part_id,),
        ):
            chunks.extend([lot["manufacturer"], lot["supplier"]])

        # 分类路径也进检索列，这样搜"电容"能捞到该分类下所有器件
        cat_path = self.tree.category_path(row["category_id"])
        if cat_path:
            chunks.append(cat_path)

        for pv in self.get_params(part_id):
            chunks.append(pv.display)
            if pv.value_num is not None:
                chunks.extend(engineering_forms(pv.value_num, pv.unit))
            else:
                chunks.append(pv.value_text)

        text = " ".join(c for c in chunks if c).lower()
        self.db.execute("UPDATE part SET search_text = ? WHERE id = ?", (text, part_id))

    def rebuild_all_search_text(self) -> int:
        """全部重建。改了参数模板或迁移数据后可以手动跑一次。"""
        rows = self.db.query("SELECT id FROM part")
        for r in rows:
            self._refresh_search_text(r["id"])
        return len(rows)
