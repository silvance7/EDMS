"""领域模型：纯数据类，不含任何 Qt / SQL 依赖。

这些类只负责"携带数据"，所有业务规则都在 service 里。
这样 domain 层可以脱离界面直接在解释器里跑测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

# 库存流水的原因枚举。定成常量而不是 Enum，是为了直接存进 SQLite 的 TEXT 列。
REASON_IN = "入库"
REASON_OUT = "出库"
REASON_COUNT = "盘点"
REASON_SCRAP = "报废"


def _from_row(cls, row: Any):
    """把 sqlite3.Row 转成 dataclass。只取数据类里声明过的字段，多余列忽略。"""
    if row is None:
        return None
    names = {f.name for f in fields(cls)}
    return cls(**{k: row[k] for k in row.keys() if k in names})


@dataclass(slots=True)
class Category:
    id: int | None = None
    name: str = ""
    parent_id: int | None = None
    sort_order: int = 0
    note: str = ""

    @classmethod
    def from_row(cls, row) -> "Category | None":
        return _from_row(cls, row)


@dataclass(slots=True)
class Location:
    id: int | None = None
    name: str = ""
    code: str = ""
    parent_id: int | None = None
    sort_order: int = 0
    note: str = ""

    @classmethod
    def from_row(cls, row) -> "Location | None":
        return _from_row(cls, row)


@dataclass(slots=True)
class Part:
    id: int | None = None
    name: str = ""
    category_id: int | None = None
    mpn: str = ""
    footprint: str = ""
    description: str = ""
    datasheet: str = ""
    keywords: str = ""
    search_text: str = ""
    min_stock: int = 0
    is_active: int = 1
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row) -> "Part | None":
        return _from_row(cls, row)


@dataclass(slots=True)
class ParamTemplate:
    id: int | None = None
    category_id: int | None = None
    name: str = ""
    unit: str = ""
    data_type: str = "num"
    sort_order: int = 0

    @classmethod
    def from_row(cls, row) -> "ParamTemplate | None":
        return _from_row(cls, row)


@dataclass(slots=True)
class PartParam:
    """一个器件在某参数模板上的取值。数值型和文本型分开存。"""

    id: int | None = None
    part_id: int | None = None
    template_id: int | None = None
    value_num: float | None = None
    value_text: str = ""

    @classmethod
    def from_row(cls, row) -> "PartParam | None":
        return _from_row(cls, row)


@dataclass(slots=True)
class PartParamView:
    """part_param JOIN param_template 的结果——界面显示需要单位，而单位在模板上。

    这两个表分开是存储层的选择；到了展示层就合并成一个对象，
    免得界面代码到处去查模板。
    """

    template_id: int = 0
    name: str = ""
    unit: str = ""
    data_type: str = "num"
    value_num: float | None = None
    value_text: str = ""
    sort_order: int = 0

    @property
    def display(self) -> str:
        """人类可读的取值，例如 "10 kΩ" / "X7R"。"""
        if self.value_num is not None:
            # %g 去掉无意义的尾随零：10000.0 -> 10000，1.50 -> 1.5
            return f"{self.value_num:g} {self.unit}".strip()
        return self.value_text

    @property
    def searchable(self) -> str:
        return f"{self.name}{self.display} {self.display}".lower()


@dataclass(slots=True)
class StockLot:
    id: int | None = None
    part_id: int | None = None
    location_id: int | None = None
    quantity: int = 0
    unit_price: float = 0.0
    currency: str = "CNY"
    purchase_date: str = ""
    supplier: str = ""
    manufacturer: str = ""
    order_no: str = ""
    batch_no: str = ""
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    # 由 JOIN 带出来的附加显示字段（不在 stock_lot 表里）
    location_label: str = ""

    @classmethod
    def from_row(cls, row) -> "StockLot | None":
        return _from_row(cls, row)


@dataclass(slots=True)
class StockLog:
    id: int | None = None
    part_id: int | None = None
    lot_id: int | None = None
    delta: int = 0
    reason: str = ""
    ref: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, row) -> "StockLog | None":
        return _from_row(cls, row)


@dataclass(slots=True)
class PartOverview:
    """列表页每行要展示的全部信息，一次查询拿齐（对应视图 v_part_overview）。"""

    id: int = 0
    name: str = ""
    category_id: int | None = None
    mpn: str = ""
    footprint: str = ""
    description: str = ""
    min_stock: int = 0
    is_active: int = 1
    total_qty: int = 0
    lot_count: int = 0
    last_purchase: str = ""
    avg_price: float = 0.0
    # 厂商与供应商是批次属性，这里是该器件下所有批次的聚合（"YAGEO,厚声"）
    manufacturers: str = ""
    suppliers: str = ""

    # 由 service 补上的附加显示字段
    category_path: str = ""
    location_summary: str = ""
    params_summary: str = ""

    # -- 状态判断，供界面标红用 -------------------------------------------

    @property
    def is_out_of_stock(self) -> bool:
        return self.total_qty <= 0

    @property
    def is_low_stock(self) -> bool:
        """库存小于等于预警值。注意 min_stock=0 时不算预警（等于没设阈值）。"""
        return self.min_stock > 0 and self.total_qty <= self.min_stock

    @property
    def stock_state(self) -> str:
        if self.is_out_of_stock:
            return "out"
        if self.is_low_stock:
            return "low"
        return "ok"

    @classmethod
    def from_row(cls, row) -> "PartOverview | None":
        if row is None:
            return None
        names = {f.name for f in fields(cls)}
        return cls(**{k: row[k] for k in row.keys() if k in names})


@dataclass(slots=True)
class SearchQuery:
    """搜索条件。所有字段都是可选的，留空表示不限制。"""

    keyword: str = ""
    category_id: int | None = None      # 含子分类
    footprint: str = ""
    location_id: int | None = None      # 含子位置
    only_low_stock: bool = False
    only_active: bool = True
    limit: int = 500
    order_by: str = "name"              # name | total_qty | updated_at | last_purchase
    order_desc: bool = False

    # 参数筛选：(模板名, 最小值, 最大值)，用于"阻值 10k~100k"这类条件
    param_ranges: list[tuple[str, float | None, float | None]] = field(default_factory=list)

    @property
    def tokens(self) -> list[str]:
        """关键词按空白切开，多个词之间是 AND 关系。

        这样 "10k 0603" 会同时要求两个词都命中，符合直觉。
        """
        return [t for t in self.keyword.lower().split() if t]
