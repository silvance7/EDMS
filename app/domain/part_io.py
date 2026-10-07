"""器件清单的导出与导入。

**导出**：把整个库存导出成一份 CSV 清单 —— 每个批次一行，可以拿去备份、
对账，也可以在 Excel 里批量修改后再导回来。
**导入**：读一份 CSV / xlsx 清单，批量建档 + 按数量入库。

格式约定（导出与导入共用同一套列名，形成闭环）::

    器件名称, 型号, 封装, 分类, 关键词, 数量, 位置, 单价, 购买日期, 厂商, 供应商, 备注

- **一行 = 一个批次**；数量为 0 的行表示器件在册但没货
- 分类 / 位置写**完整路径**（"电阻 / 贴片电阻"），导入时逐级查找，缺的自动创建
- 同一器件在文件里出现多行（多批次）时，器件级信息取第一行的非空值

导入的匹配口径（用户拍板「建档 + 入库」）：

    库里没有这个名称 -> 建档（型号 / 封装 / 关键词 / 分类都从文件里取）
    库里已有这个名称 -> **不动它的档案**，只按数量入库
                        （避免一份格式不完整的清单把好好的档案覆盖坏）

入库永远走 `StockService.add_lot` 的批次合并规则（位置 + 单价 + 日期 +
厂商 + 供应商全同才合并），不另起一套。

**逐行独立提交，不是一个大事务**：清单里个别行数据错（手打错一个数）很常见，
为一行把 99 行成功的全滚掉，用户还得从头再导一遍。行级失败进报告，
其余照常，用户按报告修完可以重新导 —— 但注意：**已入库的行重跑会重复累计**，
所以界面上的确认框要把这一点说清楚。

本模块**不 import 任何 Qt**，可以脱离界面直接测。
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.domain import Services
from app.domain.models import Part

log = logging.getLogger("edms.part_io")


# ===========================================================================
#  导出
# ===========================================================================

# 列顺序即导出顺序。改这里 = 同时改导出的格式和导入的识别（映射在下面）。
EXPORT_HEADERS = ["器件名称", "型号", "封装", "分类", "关键词",
                  "数量", "位置", "单价", "购买日期", "厂商", "供应商", "备注"]


def _format_price(value: float) -> str:
    """价格文本。与 app/ui 的 format_money 同口径 ——

    domain 层不能反向 import ui（会破坏分层），所以这里复刻三行：
    不要科学计数法，0.0086 就该写成 0.0086。
    """
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass(slots=True)
class ExportRow:
    """导出一行 = 一个批次（或"在册但无货"的器件）。"""

    name: str = ""
    mpn: str = ""
    footprint: str = ""
    category: str = ""
    keywords: str = ""
    qty: int = 0
    location: str = ""
    unit_price: float = 0.0
    purchase_date: str = ""
    manufacturer: str = ""
    supplier: str = ""
    note: str = ""

    def as_cells(self) -> list[str]:
        return [
            self.name, self.mpn, self.footprint, self.category, self.keywords,
            str(self.qty), self.location, _format_price(self.unit_price),
            self.purchase_date, self.manufacturer, self.supplier, self.note,
        ]


def collect_export_rows(svc: Services) -> list[ExportRow]:
    """把全库器件收成导出行。

    - 有在库批次的器件：每个批次一行（保真：价格 / 日期 / 厂商都在批次上）
    - 没货的器件：一行数量 0 的记录，保证导回来还是"在册"状态
    """
    parts = svc.db.query(
        "SELECT id, name, mpn, footprint, keywords, category_id FROM part "
        "ORDER BY name COLLATE NOCASE, id"
    )
    lots = svc.db.query(
        "SELECT part_id, quantity, location_id, unit_price, purchase_date, "
        "       manufacturer, supplier, note "
        "FROM stock_lot WHERE quantity > 0 ORDER BY part_id, id"
    )

    lots_by_part: dict[int, list] = {}
    for lot in lots:
        lots_by_part.setdefault(lot["part_id"], []).append(lot)

    rows: list[ExportRow] = []
    for p in parts:
        base = dict(
            name=p["name"], mpn=p["mpn"], footprint=p["footprint"],
            category=svc.tree.category_path(p["category_id"]),
            keywords=p["keywords"],
        )
        part_lots = lots_by_part.get(p["id"], [])
        if not part_lots:
            rows.append(ExportRow(**base))
            continue
        for lot in part_lots:
            rows.append(ExportRow(
                **base,
                qty=lot["quantity"],
                location=svc.tree.location_path(lot["location_id"]),
                unit_price=lot["unit_price"],
                purchase_date=lot["purchase_date"],
                manufacturer=lot["manufacturer"],
                supplier=lot["supplier"],
                note=lot["note"],
            ))
    return rows


def export_summary(rows: list[ExportRow]) -> tuple[int, int]:
    """(器件数, 行数)。器件数按名称去重。"""
    return len({r.name for r in rows}), len(rows)


def write_export_csv(path: str | Path, rows: list[ExportRow]) -> None:
    """写 CSV。utf-8-sig 是给 Excel 看的 —— 没有 BOM 中文会乱码。"""
    path = Path(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXPORT_HEADERS)
        for row in rows:
            writer.writerow(row.as_cells())
    parts, lines = export_summary(rows)
    log.info("导出器件清单：%s（%d 个器件 / %d 行）", path, parts, lines)


# ===========================================================================
#  导入 —— 读文件
# ===========================================================================

# 列名映射（宽进）。key 是"去掉所有空白 + 小写"后的表头文本 ——
# 用户手写的表头五花八门（"数量" / "Qty" / "qty "），这里全认。
_HEADER_MAP = {
    "器件名称": "name", "名称": "name", "器件": "name",
    "name": "name", "part": "name", "comment": "name",
    "型号": "mpn", "料号": "mpn", "厂商型号": "mpn",
    "mpn": "mpn", "manufacturerpart": "mpn", "manufacturerpartnumber": "mpn",
    "封装": "footprint", "footprint": "footprint", "package": "footprint",
    "分类": "category", "category": "category",
    "关键词": "keywords", "关键字": "keywords", "别名": "keywords",
    "keywords": "keywords",
    "数量": "qty", "库存": "qty", "quantity": "qty", "qty": "qty",
    "位置": "location", "存放位置": "location", "location": "location",
    "单价": "unit_price", "价格": "unit_price",
    "unitprice": "unit_price", "price": "unit_price",
    "购买日期": "purchase_date", "日期": "purchase_date",
    "purchasedate": "purchase_date", "date": "purchase_date",
    "厂商": "manufacturer", "制造商": "manufacturer",
    "manufacturer": "manufacturer",
    "供应商": "supplier", "supplier": "supplier",
    "备注": "note", "note": "note",
}

_IMPORT_FIELDS = ("name", "mpn", "footprint", "category", "keywords", "qty",
                  "location", "unit_price", "purchase_date",
                  "manufacturer", "supplier", "note")


@dataclass(slots=True)
class ImportRow:
    """导入的一行（已通过字段级校验）。"""

    line_no: int = 0        # 文件里的行号（含表头，和 Excel 里看到的一致）
    name: str = ""
    mpn: str = ""
    footprint: str = ""
    category: str = ""
    keywords: str = ""
    qty: int = 0
    location: str = ""
    unit_price: float = 0.0
    purchase_date: str = ""
    manufacturer: str = ""
    supplier: str = ""
    note: str = ""


def _norm_header(text: str) -> str:
    """表头归一化：去掉所有空白 + 转小写。"""
    return re.sub(r"[\s\u3000]+", "", (text or "").lower())


def _decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 编码无法识别（试过 utf-8 / gbk / utf-16）")


def _read_csv_with_lines(path: Path) -> tuple[list[str], list[tuple[int, list[str]]]]:
    """读 CSV，给每一行带上真实行号（csv.reader 的 line_num，处理多行字段也准）。

    行号是给报告用的 —— 出错时告诉用户"第几行"，他要在 Excel 里对得上。
    """
    reader = csv.reader(io.StringIO(_decode_csv(path.read_bytes())))
    header: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []
    for cells in reader:
        if header is None:
            if not any(c.strip() for c in cells):
                continue
            header = cells
            continue
        if not any(c.strip() for c in cells):
            continue
        rows.append((reader.line_num, cells))
    if header is None:
        raise ValueError("CSV 是空的")
    return header, rows


def read_import_rows(path: str | Path) -> tuple[list[ImportRow], list[str]]:
    """读清单文件，返回 (有效行, 错误说明)。

    行级错误不抛异常 —— 收集起来跟着报告一起给用户，能导的先导进去。
    只有"整个文件读不动"（不存在 / 认不出列名）才抛。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")

    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        # 复用 BOM 模块的手写 xlsx 解析器（zip + XML，不引 openpyxl）。
        # 行号按"表头 +1"估算：xlsx 里空行很少见，够用。
        from app.domain.bom import _read_xlsx

        header, raw_rows = _read_xlsx(path)
        paired = [(i + 2, cells) for i, cells in enumerate(raw_rows)]
    elif suffix in (".csv", ".txt", ".tsv"):
        header, paired = _read_csv_with_lines(path)
    else:
        raise ValueError(f"不支持的格式：{suffix}（支持 .csv / .xlsx）")

    return _rows_to_import(header, paired)


def _rows_to_import(
    header: list[str], paired: list[tuple[int, list[str]]]
) -> tuple[list[ImportRow], list[str]]:
    mapping: dict[int, str] = {}
    for i, h in enumerate(header):
        key = _HEADER_MAP.get(_norm_header(h))
        if key and key not in mapping.values():
            mapping[i] = key

    if "name" not in mapping.values():
        raise ValueError(
            "认不出「器件名称」列。表头至少要有一列叫「器件名称」/「名称」/ name。\n"
            f"实际读到：{header}"
        )

    rows: list[ImportRow] = []
    errors: list[str] = []
    for line_no, cells in paired:
        data = {k: "" for k in _IMPORT_FIELDS}
        for i, cell in enumerate(cells):
            key = mapping.get(i)
            if key:
                data[key] = (cell or "").strip()

        name = data["name"]
        if not name:
            errors.append(f"第 {line_no} 行：没有器件名称，已跳过")
            continue

        # -- 数量：空 = 0（只建档）；非数字报错 --
        qty_raw = data["qty"].replace(",", "")
        qty = 0
        if qty_raw:
            try:
                qty = int(float(qty_raw))
            except ValueError:
                errors.append(
                    f"第 {line_no} 行「{name}」：数量「{data['qty']}」不是数字，已跳过"
                )
                continue
            if qty < 0:
                errors.append(
                    f"第 {line_no} 行「{name}」：数量不能为负数，已跳过"
                )
                continue

        # -- 单价：空 = 0；带 ¥/$ 符号也认 --
        price_raw = data["unit_price"].replace(",", "").strip().lstrip("¥￥$")
        price = 0.0
        if price_raw:
            try:
                price = float(price_raw)
            except ValueError:
                errors.append(
                    f"第 {line_no} 行「{name}」：单价「{data['unit_price']}」不是数字，已跳过"
                )
                continue
            if price < 0:
                errors.append(
                    f"第 {line_no} 行「{name}」：单价不能为负数，已跳过"
                )
                continue

        rows.append(ImportRow(
            line_no=line_no,
            name=name,
            mpn=data["mpn"],
            footprint=data["footprint"],
            category=data["category"],
            keywords=data["keywords"],
            qty=qty,
            location=data["location"],
            unit_price=price,
            purchase_date=data["purchase_date"],
            manufacturer=data["manufacturer"],
            supplier=data["supplier"],
            note=data["note"],
        ))
    return rows, errors


# ===========================================================================
#  导入 —— 树路径的查找与创建
# ===========================================================================

def _split_path(text: str, tight: bool) -> list[str]:
    """把 "电阻 / 贴片电阻" 拆成 ["电阻", "贴片电阻"]。

    两种解释：

    - `tight=True`  — 只认 " / "（**斜杠两边都有空白**）。这是导出的规范格式。
    - `tight=False` — 连 "电阻/贴片电阻"（没空格）也拆。用户手打的路径大概率不规范。

    为什么不只用一种：库里真实存在「芯片/IC」「三极管/MOS」这种**名字里带斜杠**
    的分类，它导出的路径就是 "芯片/IC"（斜杠无空格）。只按宽松规则拆，
    会被切成 ["芯片", "IC"]，凭空建出两个错的分类。所以两种解释都算，
    用哪个见 `_walk_path` —— **能在树里走通更多级的那种才算数**。
    """
    pattern = r"\s+/\s+" if tight else r"\s*/\s*"
    return [p.strip() for p in re.split(pattern, text or "") if p.strip()]


class _TreeIndex:
    """分类 / 位置树的内存索引：父节点 -> {名字小写: id}。

    导入一批清单要把每个路径段都查一遍库，全量拉下来建索引比逐行查询省。
    `add_virtual` 是预演用的：给"将要创建"的节点分配负数假 id，
    这样文件里后续行再引用同一路径时不会重复算成"待创建"。
    """

    def __init__(self, nodes: list[tuple[int, str, int | None]]) -> None:
        self._children: dict[int | None, dict[str, int]] = {}
        for node_id, name, parent_id in nodes:
            self._children.setdefault(parent_id, {})[name.strip().casefold()] = node_id
        self._next_virtual = -1

    @classmethod
    def from_categories(cls, svc: Services) -> "_TreeIndex":
        return cls([(c.id, c.name, c.parent_id) for c in svc.tree.list_categories()])

    @classmethod
    def from_locations(cls, svc: Services) -> "_TreeIndex":
        return cls([(l.id, l.name, l.parent_id) for l in svc.tree.list_locations()])

    def find(self, parent_id: int | None, name: str) -> int | None:
        return self._children.get(parent_id, {}).get(name.strip().casefold())

    def add(self, parent_id: int | None, name: str, node_id: int) -> None:
        self._children.setdefault(parent_id, {})[name.strip().casefold()] = node_id

    def add_virtual(self, parent_id: int | None, name: str) -> int:
        virtual = self._next_virtual
        self._next_virtual -= 1
        self.add(parent_id, name, virtual)
        return virtual


def _match_depth(index: _TreeIndex, parts: list[str]) -> int:
    """从头开始能连续命中的级数（用于给两种路径解释打分）。"""
    parent: int | None = None
    depth = 0
    for seg in parts:
        found = index.find(parent, seg)
        if found is None:
            break
        parent = found
        depth += 1
    return depth


def _choose_parts(index: _TreeIndex, text: str) -> list[str]:
    """选路径的拆分解释：能走通更多级的那种。

    - "芯片/IC"            -> tight 解释一下命中 1 级，宽松解释是 ["芯片","IC"] 命中 0 级
                             -> 用 tight（整串就是一个已存在的分类名）
    - "新柜子/第三层"（手写）-> 两种都是 0 级 -> 平局，选宽松的（拆成两级来建）
    - "电阻 / 贴片电阻"     -> 两种解释相同，直接用
    """
    parts_tight = _split_path(text, tight=True)
    parts_loose = _split_path(text, tight=False)
    if parts_tight == parts_loose:
        return parts_tight
    if _match_depth(index, parts_tight) > _match_depth(index, parts_loose):
        return parts_tight
    return parts_loose


def _walk_path(
    index: _TreeIndex,
    text: str,
    created: list[str],
    create=None,
) -> int | None:
    """逐级解析路径，返回叶子节点 id。空路径返回 None（"未分类" / "未指定位置"）。

    create=None 时是**预演**：只把缺失的路径段标记进 created（虚拟节点），不落库。
    create=callable 时真建：`create(name, parent_id) -> id`。
    """
    if not (text or "").strip():
        return None
    parts = _choose_parts(index, text)

    parent: int | None = None
    trail: list[str] = []
    for seg in parts:
        trail.append(seg)
        found = index.find(parent, seg)
        if found is None:
            if create is None:
                found = index.add_virtual(parent, seg)
            else:
                found = create(seg, parent)
                index.add(parent, seg, found)
            created.append(" / ".join(trail))
        parent = found
    return parent


# ===========================================================================
#  导入 —— 预演与执行
# ===========================================================================

@dataclass(slots=True)
class ImportPlan:
    """导入前的预演结果 —— 给用户看清楚"点下去会发生什么"。"""

    rows: list[ImportRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)         # 解析阶段的错误
    new_parts: int = 0                                      # 将新建的器件数
    existing_parts: int = 0                                 # 库中已存在的器件数
    stock_lines: int = 0                                    # 数量 > 0 的行数
    stock_qty: int = 0                                      # 将入库的颗数
    new_categories: list[str] = field(default_factory=list)  # 将自动创建的分类（完整路径）
    new_locations: list[str] = field(default_factory=list)   # 将自动创建的位置


def _name_key(name: str) -> str:
    return name.strip().casefold()


def plan_import(svc: Services, rows: list[ImportRow],
                errors: list[str] | None = None) -> ImportPlan:
    """只读预演：算清楚会建几个档案、入多少库、自动建哪些分类/位置。

    errors 是 read_import_rows 收集的**解析阶段**错误（行号 + 原因），
    带进来是给界面预览用的（"另有 N 行有问题，导入时会跳过"）。
    """
    plan = ImportPlan(rows=rows, errors=list(errors or []))

    existing = {_name_key(r["name"]) for r in svc.db.query("SELECT name FROM part")}
    touched_existing: set[str] = set()
    seen_new: set[str] = set()

    for row in rows:
        key = _name_key(row.name)
        if key in existing:
            touched_existing.add(key)
        elif key not in seen_new:
            seen_new.add(key)
        if row.qty > 0:
            plan.stock_lines += 1
            plan.stock_qty += row.qty

    plan.new_parts = len(seen_new)
    plan.existing_parts = len(touched_existing)

    cat_index = _TreeIndex.from_categories(svc)
    loc_index = _TreeIndex.from_locations(svc)
    for row in rows:
        _walk_path(cat_index, row.category, plan.new_categories)
        _walk_path(loc_index, row.location, plan.new_locations)
    return plan


@dataclass(slots=True)
class ImportReport:
    """导入后的实际结果 —— 一行一行摆给用户看，不要"成功"两个字打发。"""

    total_lines: int = 0
    created_parts: int = 0
    existing_parts: int = 0
    lots_in: int = 0                                        # 实际入库的行数
    qty_in: int = 0                                         # 实际入库的颗数
    skipped_stock_lines: int = 0                            # 有数量但按开关跳过入库的行
    categories_created: list[str] = field(default_factory=list)
    locations_created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def execute_import(svc: Services, rows: list[ImportRow],
                   with_stock: bool = True) -> ImportReport:
    """按「建档 + 入库」执行。逐行独立提交，个别行失败不影响其余行。

    with_stock=False 时只建档、不入库（面板上的开关）。
    """
    report = ImportReport(total_lines=len(rows))

    existing: dict[str, int] = {
        _name_key(r["name"]): r["id"] for r in svc.db.query("SELECT id, name FROM part")
    }
    created_keys: set[str] = set()
    seen_existing: set[str] = set()

    cat_index = _TreeIndex.from_categories(svc)
    loc_index = _TreeIndex.from_locations(svc)

    def create_category(name: str, parent: int | None) -> int:
        return svc.tree.create_category(name, parent)

    def create_location(name: str, parent: int | None) -> int:
        return svc.tree.create_location(name, parent)

    for row in rows:
        key = _name_key(row.name)
        try:
            part_id = existing.get(key)

            if part_id is None:
                # 建档：分类路径从文件里取，缺的自动创建
                category_id = _walk_path(
                    cat_index, row.category, report.categories_created,
                    create=create_category,
                )
                part_id = svc.parts.create(Part(
                    name=row.name,
                    category_id=category_id,
                    mpn=row.mpn,
                    footprint=row.footprint,
                    keywords=row.keywords,
                ))
                existing[key] = part_id
                created_keys.add(key)
                report.created_parts += 1
            elif key not in created_keys:
                # 已存在的器件不改档案，只记账（本文件刚建的也不算"原有"）
                seen_existing.add(key)

            if row.qty > 0:
                if not with_stock:
                    report.skipped_stock_lines += 1
                    continue
                location_id = _walk_path(
                    loc_index, row.location, report.locations_created,
                    create=create_location,
                )
                svc.stock.add_lot(
                    part_id, row.qty,
                    location_id=location_id,
                    unit_price=row.unit_price,
                    purchase_date=row.purchase_date,
                    supplier=row.supplier,
                    manufacturer=row.manufacturer,
                    note=row.note,
                )
                report.lots_in += 1
                report.qty_in += row.qty

        except Exception as exc:  # noqa: BLE001
            log.exception("导入失败：第 %s 行「%s」", row.line_no, row.name)
            report.errors.append(f"第 {row.line_no} 行「{row.name}」：{exc}")

    report.existing_parts = len(seen_existing)
    log.info(
        "导入器件清单：新建 %d 个器件，入库 %d 行 / %d 颗，自动创建分类 %d 个、位置 %d 个%s",
        report.created_parts, report.lots_in, report.qty_in,
        len(report.categories_created), len(report.locations_created),
        f"，{len(report.errors)} 行失败" if report.errors else "",
    )
    return report


__all__ = [
    "EXPORT_HEADERS",
    "ExportRow",
    "collect_export_rows",
    "export_summary",
    "write_export_csv",
    "ImportRow",
    "read_import_rows",
    "ImportPlan",
    "plan_import",
    "ImportReport",
    "execute_import",
]
