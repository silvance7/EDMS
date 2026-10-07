"""器件列表的表格模型。

用 QAbstractTableModel 而不是 QTableWidget，是因为 Model/View 把"数据"和"怎么画"
彻底分开：几万行也只创建可见行的 delegate，滚动不卡；
而且排序、筛选全部落在 SQL 层，不在内存里折腾。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont

from app.domain.models import PartOverview
from app.ui import format_money, theme


class Column:
    __slots__ = ("key", "title", "width", "align")

    def __init__(self, key: str, title: str, width: int, align=Qt.AlignLeft | Qt.AlignVCenter):
        self.key = key
        self.title = title
        self.width = width
        self.align = align


_RIGHT = Qt.AlignRight | Qt.AlignVCenter
_CENTER = Qt.AlignHCenter | Qt.AlignVCenter

COLUMNS: list[Column] = [
    Column("name", "名称", 250),
    Column("footprint", "封装", 84, _CENTER),
    Column("total_qty", "库存", 68, _RIGHT),
    Column("location_summary", "存放位置", 190),
    Column("avg_price", "均价(¥)", 84, _RIGHT),
    Column("last_purchase", "最近采购", 96, _CENTER),
    Column("mpn", "型号", 150),
    Column("category_path", "分类", 130),
]


class PartTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[PartOverview] = []

    # -- 数据装载 ----------------------------------------------------------

    def set_rows(self, rows: list[PartOverview]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, row: int) -> PartOverview | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    @property
    def rows(self) -> list[PartOverview]:
        return self._rows

    # -- QAbstractTableModel 接口 ------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMNS[section].title
        if role == Qt.TextAlignmentRole:
            return int(COLUMNS[section].align)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self._rows[index.row()]
        col = COLUMNS[index.column()]

        # ---- 显示内容 ----
        if role == Qt.DisplayRole:
            return self._display(item, col.key)

        # ---- 对齐 ----
        if role == Qt.TextAlignmentRole:
            return int(col.align)

        # ---- 前景色：缺货红、低库存琥珀 ----
        if role == Qt.ForegroundRole:
            p = theme.pal()
            if col.key in ("total_qty", "location_summary"):
                if item.is_out_of_stock:
                    return QBrush(QColor(p.fg_out))
                if item.is_low_stock:
                    return QBrush(QColor(p.fg_low))
            if col.key in ("avg_price", "last_purchase", "mpn", "category_path"):
                return QBrush(QColor(p.fg_muted))

        # ---- 背景色：整行提示 ----
        if role == Qt.BackgroundRole:
            p = theme.pal()
            if item.is_out_of_stock:
                return QBrush(QColor(p.bg_out))
            if item.is_low_stock:
                return QBrush(QColor(p.bg_low))

        # ---- 字重：紧急的行加粗库存数字 ----
        if role == Qt.FontRole and col.key == "total_qty":
            if item.is_out_of_stock or item.is_low_stock:
                f = QFont()
                f.setBold(True)
                return f

        # ---- 提示气泡 ----
        if role == Qt.ToolTipRole:
            return self._tooltip(item)

        # ---- 排序：直接返回原始值，选中列时让 SQL 去排序，这里只做兜底 ----
        if role == Qt.UserRole:
            return getattr(item, col.key, "")

        return None

    # -- 内部 --------------------------------------------------------------

    @staticmethod
    def _display(item: PartOverview, key: str):
        value = getattr(item, key, "")

        if key == "total_qty":
            return f"{int(item.total_qty)}"
        if key == "avg_price":
            # 均价也可能小到 0.000086，用 format_money 别用 :.4f（会四舍五入丢精度）
            return format_money(item.avg_price) if item.avg_price else "—"
        if key == "last_purchase":
            return item.last_purchase or "—"
        if key == "name":
            # 缺货 / 低库存加个前缀标记，色弱用户也能分辨
            if item.is_out_of_stock:
                return f"⊘ {item.name}"
            if item.is_low_stock:
                return f"! {item.name}"
            return item.name
        if key == "mpn":
            return value or "—"
        if key == "category_path":
            return value or "—"
        return value

    @staticmethod
    def _tooltip(item: PartOverview) -> str:
        lines = [f"<b>{item.name}</b>"]
        if item.category_path:
            lines.append(f"分类：{item.category_path}")
        if item.mpn:
            maker = f"（{item.manufacturers}）" if item.manufacturers else ""
            lines.append(f"型号：{item.mpn}{maker}")
        if item.footprint:
            lines.append(f"封装：{item.footprint}")
        lines.append(f"总库存：<b>{item.total_qty}</b>　批次：{item.lot_count}")
        lines.append(f"存放：{item.location_summary}")
        if item.min_stock:
            lines.append(f"预警阈值：{item.min_stock}")
        if item.description:
            lines.append(f"备注：{item.description}")
        return "<br>".join(lines)
