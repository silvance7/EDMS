"""BOM 操作对话框：比对 / 导出 / 导入。

三个页签共用一个入口（主窗口的「BOM 操作」按钮）：

    比对   选 BOM 文件 -> 自动匹配 -> 逐行核对 -> 一键全出库（原有功能，原样保留在第一个页签）
    导出   把当前库存导成一份 CSV 器件清单（见 app/ui/bom_io.py）
    导入   从清单文件批量建档 + 入库（见 app/ui/bom_io.py）

比对页两个刻意的设计：

1. **「参与出库」是显式勾选的**，默认只勾上高置信度的行（数值+封装/型号/供应商编号）。
   人工确认级别的行（封装不符、仅关键词）**默认不勾** —— 弱信号不该自动参与扣库存。

2. **出库前必做预检**。按你选定的策略「全有才出」，任何一行库存不足就整批取消，
   并且把**所有**不足项一次列全（不是只报第一个），方便你一次把料补齐。
"""

from __future__ import annotations

import logging

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.domain import Services
from app.domain.bom import BomMatch, BomMatcher, CostLine, CostReport, build_cost, read_bom
from app.domain.stock_service import BulkInsufficientStockError
from app.ui import format_money, format_money_total, theme
from app.ui.bom_io import BomExportPanel, BomImportPanel

log = logging.getLogger("edms.ui.bom_dialog")

COL_CHECK = 0
COL_INDEX = 1
COL_QTY = 2
COL_LABEL = 3
COL_FOOTPRINT = 4
COL_STATE = 5
COL_PART = 6
COL_STOCK = 7

_HEADERS = ["参与", "行", "数量", "值 / Comment", "BOM 封装",
            "状态", "匹配到的器件（按匹配度排序）", "现有库存"]

# 状态图标三态。放角色里是为了让 _update_summary 能直接读回来。
_STATE_ROLE = Qt.UserRole + 10

_STATE_ICONS: dict[str, QIcon] = {}


def _make_state_icon(kind: str, size: int = 32) -> QIcon:
    """画状态图标：绿勾 / 琥珀三角 / 红叉。

    用代码画而不是往仓库塞图片：不增加打包体积，也能跟着深浅色主题走。

        绿勾  完全匹配 —— 强参数（封装/数值）和弱参数（型号/名称）全对上
        琥珀  有关键项没对上 —— 点它才展开原因，平时不占地方
        红叉  库存里没有能对上的器件
    """
    pal = theme.pal()
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    margin = size * 0.08
    box = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)

    if kind == "ok":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#3FB27F") if pal.is_dark else QColor("#2E9E5B"))
        painter.drawEllipse(box)

        pen = QPen(QColor("#FFFFFF"), size * 0.11)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        tick = QPainterPath()
        tick.moveTo(size * 0.29, size * 0.52)
        tick.lineTo(size * 0.44, size * 0.67)
        tick.lineTo(size * 0.72, size * 0.35)
        painter.drawPath(tick)

    elif kind == "warn":
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#E8B563") if pal.is_dark else QColor("#D08A00"))
        painter.drawPolygon(QPolygonF([
            QPointF(size * 0.50, margin),
            QPointF(size - margin, size - margin),
            QPointF(margin, size - margin),
        ]))

        pen = QPen(QColor("#1E1E1E") if pal.is_dark else QColor("#FFFFFF"), size * 0.10)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(size * 0.50, size * 0.40),
                         QPointF(size * 0.50, size * 0.64))
        painter.drawPoint(QPointF(size * 0.50, size * 0.76))

    else:  # miss
        pen = QPen(QColor("#FF7B72") if pal.is_dark else QColor("#C0392B"), size * 0.13)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(size * 0.32, size * 0.32),
                         QPointF(size * 0.68, size * 0.68))
        painter.drawLine(QPointF(size * 0.68, size * 0.32),
                         QPointF(size * 0.32, size * 0.68))

    painter.end()
    return QIcon(pixmap)


def state_icons() -> dict[str, QIcon]:
    """生成后缓存一份 —— 每次刷新重画 40 个图标是白费力气。"""
    if not _STATE_ICONS:
        for kind in ("ok", "warn", "miss"):
            _STATE_ICONS[kind] = _make_state_icon(kind)
    return _STATE_ICONS


class BomDialog(QDialog):
    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = services
        self._matches: list[BomMatch] = []
        self._all_parts: list[tuple[int, str]] = []
        # part_id -> PartOverview。单价（avg_price）和批次数（lot_count）都在这儿，
        # 总价和成本明细都从这一份快照算 —— 改完价要重建（见 _reload_overviews）。
        self._overview: dict[int, object] = {}
        self._cost: CostReport = CostReport()
        self._bom_path: Path | None = None

        self.setWindowTitle("BOM 操作")
        self.resize(1180, 720)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        # 三个页签共用一个入口：比对（原有功能）/ 导出 / 导入。
        # 「比对」页的内容就是本对话框原来的全部界面，一个控件都没少，
        # 只是挂到了 QTabWidget 的第一页上 —— 测试和用户习惯都保持一致。
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ---- 页签 1：比对 ----
        match_page = QWidget()
        mv = QVBoxLayout(match_page)
        mv.setContentsMargins(10, 10, 10, 10)
        mv.setSpacing(8)

        mv.addWidget(self._build_header())
        self.lbl_summary = QLabel("还没有载入 BOM。")
        self.lbl_summary.setProperty("hint", True)
        mv.addWidget(self.lbl_summary)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        # 点状态图标才展开原因 —— 平时不占地方，需要时又随手可得
        self.table.cellClicked.connect(self._on_cell_clicked)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        mv.addWidget(self.table, 1)

        mv.addWidget(self._build_footer())
        self.tabs.addTab(match_page, "比对")

        # ---- 页签 2 / 3：导出 / 导入 ----
        self.export_panel = BomExportPanel(services)
        self.tabs.addTab(self.export_panel, "导出")

        self.import_panel = BomImportPanel(services)
        # 导入进来新器件后，「比对」页的器件下拉框缓存要跟上
        self.import_panel.imported.connect(self._on_parts_imported)
        self.tabs.addTab(self.import_panel, "导入")

        # ---- 底部：关闭（三个页签共用，放页签外面）----
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        bottom.addWidget(btn_close)
        root.addLayout(bottom)

        self._load_all_parts()

    # ==================================================================
    #  界面搭建
    # ==================================================================

    def _build_header(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self.lbl_file = QLabel("（未选择文件）")
        self.lbl_file.setProperty("hint", True)
        h.addWidget(self.lbl_file, 1)

        btn = QPushButton("选择 BOM 文件…")
        btn.setProperty("accent", True)
        btn.clicked.connect(self.choose_file)
        h.addWidget(btn)

        return bar

    def _build_footer(self) -> QWidget:
        bar = QWidget()
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        # ---- 总钱数（右下角，点开看逐行明细）----
        # 颜色说的是"这个钱算得全不全"，跟库存告警是两条轴：
        #   绿  = 每行都有价、也都匹配上了
        #   黄  = 还差价格或还有行没匹配，算出来的只是**不完全**的数
        cost_row = QHBoxLayout()
        cost_row.setContentsMargins(0, 0, 0, 0)
        self.btn_cost = QPushButton("总价 —")
        self.btn_cost.setProperty("link", True)
        self.btn_cost.setCursor(Qt.PointingHandCursor)
        self.btn_cost.setToolTip(
            "点击查看逐行成本明细。\n"
            "库里没记价格的器件可以在明细里直接补上，会写回数据库。"
        )
        self.btn_cost.clicked.connect(self.show_cost)
        self.btn_cost.setEnabled(False)
        cost_row.addStretch(1)
        cost_row.addWidget(self.btn_cost)
        outer.addLayout(cost_row)

        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self.chk_all = QCheckBox("全选 / 全不选")
        self.chk_all.stateChanged.connect(self._on_check_all)
        h.addWidget(self.chk_all)

        h.addStretch(1)

        self.btn_create = QPushButton("未匹配行一键建档")
        self.btn_create.setToolTip(
            "把未匹配的 BOM 行建成器件档案（库存 0、预警阈值=需求量）。\n"
            "同时把 LCSC 编号和厂商型号写进关键词，下次匹配直接走最高置信度通道。"
        )
        self.btn_create.clicked.connect(self.create_missing_parts)
        h.addWidget(self.btn_create)

        self.btn_withdraw = QPushButton("一键全出库")
        self.btn_withdraw.setProperty("accent", True)
        self.btn_withdraw.setToolTip("只处理已勾选且有匹配器件的行。任何一行库存不足，整批取消。")
        self.btn_withdraw.clicked.connect(self.run_withdraw)
        h.addWidget(self.btn_withdraw)

        # 「关闭」按钮不在这里 —— 三个页签共用，挂在对话框底部（见 __init__）

        outer.addLayout(h)
        return bar

    # ==================================================================
    #  载入
    # ==================================================================

    def _load_all_parts(self) -> None:
        """一次把全部器件捞进来，同时缓存每个器件的信息（均价 / 批次数）。

        下拉框的兜底选项和成本折算共用这一份查询结果，别查两遍。
        """
        from app.domain.models import SearchQuery
        rows = self.svc.search.search(SearchQuery(limit=5000))
        self._overview = {r.id: r for r in rows}
        self._all_parts = sorted(((r.id, r.name) for r in rows), key=lambda x: x[1])

    def _on_parts_imported(self) -> None:
        """「导入」页签导完器件后刷新缓存。

        只刷新器件清单（下拉框能选到新器件），**不**重新跑匹配 ——
        当前比对表里可能有用户手动的改选，自动重刷会把它弄丢。
        导出页签的库存统计也要跟着更新（器件变多了）。
        """
        self._load_all_parts()
        self.export_panel.refresh_stats()

    def choose_file(self) -> None:
        start = config.get("bom_dir") or ""
        if not start or not Path(start).exists():
            # 项目里的 bom/ 目录作为默认起点（打包后不存在就退回用户主目录）
            project_bom = Path(__file__).resolve().parents[2] / "bom"
            start = str(project_bom if project_bom.exists() else Path.home())

        path, _ = QFileDialog.getOpenFileName(
            self, "选择 BOM 文件", start, "BOM 文件 (*.xlsx *.xlsm *.csv);;所有文件 (*)"
        )
        if path:
            self.load(path)

    def load(self, path: str | Path) -> None:
        try:
            lines = read_bom(path)
        except Exception as exc:  # noqa: BLE001
            log.exception("读取 BOM 失败")
            QMessageBox.critical(self, "读取失败", str(exc))
            return

        self._bom_path = Path(path)
        config.set_value("bom_dir", str(self._bom_path.parent))
        self.lbl_file.setText(f"{self._bom_path.name}　（{len(lines)} 行）")

        matcher = BomMatcher(self.svc)
        self._matches = matcher.match(lines)
        self._populate()
        self._update_summary()

    # ==================================================================
    #  填表
    # ==================================================================

    def _populate(self) -> None:
        self.table.setRowCount(len(self._matches))

        for row, match in enumerate(self._matches):
            line = match.line

            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            # 只有「完全匹配 + 库存够」才默认勾选
            check.setCheckState(Qt.Checked if match.ready else Qt.Unchecked)
            check.setToolTip("勾上才会参与「一键全出库」")
            self.table.setItem(row, COL_CHECK, check)

            self.table.setItem(row, COL_INDEX, QTableWidgetItem(str(line.index)))
            self.table.setItem(row, COL_QTY, QTableWidgetItem(str(line.qty)))
            self.table.setItem(row, COL_LABEL, QTableWidgetItem(line.label))
            self.table.setItem(row, COL_FOOTPRINT, QTableWidgetItem(line.footprint))

            state_item = QTableWidgetItem()
            state_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, COL_STATE, state_item)

            combo = QComboBox()
            self._fill_combo(combo, match)
            combo.currentIndexChanged.connect(
                lambda _i, r=row, c=combo: self._on_part_changed(r, c)
            )
            self.table.setCellWidget(row, COL_PART, combo)

            self.table.setItem(row, COL_STOCK, QTableWidgetItem(""))
            self._refresh_row(row)

        # 列宽合计要留余量，否则最右边的「现有库存」会被挤出可视区
        for col, width in (
            (COL_CHECK, 42), (COL_INDEX, 40), (COL_QTY, 48), (COL_LABEL, 180),
            (COL_FOOTPRINT, 190), (COL_STATE, 44), (COL_PART, 300),
            (COL_STOCK, 96),
        ):
            self.table.setColumnWidth(col, width)
        # 这一句必须在 setColumnWidth 之后，否则"最后一段自动伸展"会被覆盖掉
        self.table.horizontalHeader().setStretchLastSection(True)

    def _fill_combo(self, combo: QComboBox, match: BomMatch) -> None:
        """候选按匹配度从高到低全部列出来，用户自己挑。

        不用 QComboBox 自带排序，因为顺序本身就是信息。
        条目上**不显示分数** —— 分数是内部排序用的，写成"78 分"用户也没法拿它做判断。
        """
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("（未指定）", None)

        if match.candidates:
            icons = state_icons()
            for cand in match.candidates:
                combo.addItem(cand.part_name, cand.part_id)
                combo.setItemData(
                    combo.count() - 1,
                    icons["ok" if cand.exact else "warn"],
                    Qt.DecorationRole,
                )
                combo.setItemData(
                    combo.count() - 1,
                    "完全匹配" if cand.exact else "未完全匹配（点状态图标看原因）",
                    Qt.ToolTipRole,
                )
            combo.insertSeparator(combo.count())

        # 兜底：候选里没有的也允许手动指定
        listed = {c.part_id for c in match.candidates}
        for pid, name in self._all_parts:
            if pid not in listed:
                combo.addItem(name, pid)

        index = combo.findData(match.part_id)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _selected_part_id(self, row: int) -> int | None:
        widget = self.table.cellWidget(row, COL_PART)
        return widget.currentData() if isinstance(widget, QComboBox) else None

    def _on_part_changed(self, row: int, combo: QComboBox) -> None:
        # 把界面的改选**写回模型**，否则 match.state / score 还是旧的，
        # 汇总和默认勾选都会对不上
        self._matches[row].part_id = combo.currentData()
        self._refresh_row(row)
        self._update_summary()

    def _refresh_row(self, row: int) -> None:
        """刷新「状态图标」和「现有库存」两列。

        状态图标只表达**匹配**情况（完全匹配 / 有关键项没对上 / 缺料）。
        库存够不够是另一条轴，用库存列自己的颜色表达，两者不混在一起 ——
        混在一起用户就分不清"是没这个料"还是"有料但不够"。
        """
        p = theme.pal()
        icons = state_icons()
        match = self._matches[row]
        qty = match.line.qty

        if match.part_id is None:
            state = "missing"
            stock_text = "—"
            stock_colour = p.fg_out
        else:
            state = "ok" if match.exact else "warn"
            stock_text = f"{match.available} / 需 {qty}"
            stock_colour = p.fg_normal if match.enough else p.fg_low

        state_item = self.table.item(row, COL_STATE)
        state_item.setData(_STATE_ROLE, state)
        if state == "missing":
            state_item.setIcon(icons["miss"])
            state_item.setToolTip("库存里没有能对上的器件。点击查看说明")
        elif state == "warn":
            state_item.setIcon(icons["warn"])
            state_item.setToolTip("有关键项没对上。点击查看原因")
        else:
            state_item.setIcon(icons["ok"])
            state_item.setToolTip("完全匹配。点击查看匹配依据")

        stock_item = self.table.item(row, COL_STOCK)
        stock_item.setText(stock_text)
        stock_item.setForeground(QColor(stock_colour))
        stock_item.setToolTip(match.note)

    # ------------------------------------------------------------------
    #  点状态图标看原因
    # ------------------------------------------------------------------

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column == COL_STATE:
            self.show_reason(row)

    def show_reason(self, row: int) -> None:
        """把「为什么是这个状态」摊开给用户看。

        平时只占一个图标的宽度，需要时点一下就有 ——
        常驻一列长文字反而没人看，还占掉真正有用的列的位置。
        """
        match = self._matches[row]
        chosen = match.selected

        lines = [f"第 {match.line.index} 行　{match.line.label}"]
        if match.line.footprint:
            lines.append(f"BOM 封装：{match.line.footprint}")
        lines.append("")

        if match.part_id is None:
            lines.append("库存里没有能对上的器件。")
            if match.near_miss:
                lines.append("")
                lines.append("库里有相近的，但强参数（封装 / 参数）不符，")
                lines.append("所以没有列入候选：")
                lines.extend(f"　· {item}" for item in match.near_miss)
                lines.append("")
                lines.append("如果确认是同一个东西，在右侧下拉框里手动指定即可。")
        else:
            lines.append(f"匹配器件：{chosen.part_name}")
            lines.append(f"库里封装：{chosen.part_footprint or '（未填）'}")
            lines.append("")
            if chosen.gaps:
                lines.append("以下弱参数没对上，需要你判断：")
                lines.extend(f"　· {gap}" for gap in chosen.gaps)
            else:
                lines.append("完全匹配：封装、参数、型号、名称全部对上。")

            if chosen.evidence:
                lines.append("")
                lines.append("匹配依据：")
                lines.extend(f"　· {item}" for item in chosen.evidence)

            lines.append("")
            lines.append(f"现有库存：{chosen.available}　　BOM 需要：{match.line.qty}")
            if not match.enough:
                lines.append("→ 库存不足，补齐后才能参与出库。")

        QMessageBox.information(self, "匹配说明", "\n".join(lines))

    # ==================================================================
    #  汇总
    # ==================================================================

    def _update_summary(self) -> None:
        if not self._matches:
            self.lbl_summary.setText("还没有载入 BOM。")
            self._update_cost()
            return

        total = len(self._matches)
        total_qty = sum(m.line.qty for m in self._matches)
        exact = sum(1 for m in self._matches if m.exact)
        unconfirmed = sum(1 for m in self._matches
                          if m.part_id is not None and not m.exact)
        missing = sum(1 for m in self._matches if m.part_id is None)

        checked = self._checked_rows()
        checked_qty = sum(self._matches[r].line.qty for r in checked)
        shortage = [r for r in checked if not self._matches[r].enough]
        unchecked_ready = [
            r for r in range(total)
            if self._matches[r].ready
            and self.table.item(r, COL_CHECK).checkState() != Qt.Checked
        ]

        # 三档计数放最前面，一眼就能看出这套板子能不能做
        # （和立创 BOM 配单的「完全匹配 / 待确认 / 无法匹配」是同一个分法）
        parts = [
            f"{total} 行 / 合计 {total_qty} 颗",
            f"完全匹配 {exact} 行",
            f"待确认 {unconfirmed} 行",
            f"缺料 {missing} 行",
        ]
        if checked:
            parts.append(f"已勾选 {len(checked)} 行（{checked_qty} 颗）")
        if shortage:
            parts.append(f"勾选行中 {len(shortage)} 行库存不足")
        if unchecked_ready:
            parts.append(f"另有 {len(unchecked_ready)} 行可就绪但未勾选")
        self.lbl_summary.setText("　·　".join(parts))
        self._update_cost()

    def _checked_rows(self) -> list[int]:
        return [
            r for r in range(len(self._matches))
            if self.table.item(r, COL_CHECK).checkState() == Qt.Checked
        ]

    def _on_check_all(self, state: int) -> None:
        target = Qt.Checked if state == Qt.Checked else Qt.Unchecked
        self.table.blockSignals(True)
        for row in range(len(self._matches)):
            # 没指定器件的行勾了也没用，直接跳过
            if target == Qt.Checked and self._selected_part_id(row) is None:
                continue
            self.table.item(row, COL_CHECK).setCheckState(target)
        self.table.blockSignals(False)
        self._update_summary()

    # ==================================================================
    #  总钱数
    # ==================================================================

    def _update_cost(self) -> None:
        """重算总价，刷新右下角那个可点的数字。

        单价用**持仓均价**（`avg_price`），一个器件多个批次价格不同时已按数量加权。
        单价是快照，所以改完价必须调 `_reload_overviews()` 重算，否则显示的是旧数。
        """
        if not self._matches:
            self._cost = CostReport()
            self.btn_cost.setText("总价 —")
            self.btn_cost.setStyleSheet(f"color: {theme.pal().fg_muted};")
            self.btn_cost.setEnabled(False)
            return

        self._cost = build_cost(self._matches, self._overview.get)
        n_missing = len(self._cost.missing)
        n_unmatched = len(self._cost.unmatched)

        text = f"总价 ¥{format_money_total(self._cost.total)}"
        if n_missing:
            text += f"　·　{n_missing} 项待补价"
        if n_unmatched:
            text += f"　·　{n_unmatched} 行未匹配"

        pal = theme.pal()
        self.btn_cost.setText(text)
        self.btn_cost.setStyleSheet(
            f"color: {pal.fg_ok if self._cost.complete else pal.fg_attention};"
        )
        self.btn_cost.setEnabled(True)
        self.btn_cost.setToolTip(
            f"已加到价格的共 {len(self._cost.lines) - n_missing - n_unmatched} 行。\n"
            f"{'全都算得出来。' if self._cost.complete else '还有算不出来的部分，点开看逐行明细。'}\n"
            "—— 库里没记价格的可以在明细里直接补，会写回数据库。"
        )

    def _reload_overviews(self) -> None:
        """改完价重新拉一遍器件信息（均价 / 批次数），然后重算总价。"""
        self._load_all_parts()
        self._update_cost()

    def show_cost(self) -> None:
        """点总价：弹出逐行成本明细。改完价回来要把总价刷新掉。"""
        if not self._matches:
            return
        dlg = BomCostDialog(self.svc, self._cost, self)
        dlg.exec()
        if dlg.changed:
            self._reload_overviews()

    # ==================================================================
    #  一键全出库
    # ==================================================================

    def run_withdraw(self) -> None:
        rows = [r for r in self._checked_rows() if self._selected_part_id(r) is not None]
        if not rows:
            QMessageBox.information(
                self, "没有可出库的行",
                "请先勾选要出库的行，并确保它们都指定了匹配的器件。",
            )
            return

        requests: list[tuple[int, int, str]] = []
        label_by_part: dict[int, str] = {}
        stem = self._bom_path.name if self._bom_path else "BOM"

        for row in rows:
            match = self._matches[row]
            pid = self._selected_part_id(row)
            name = self.svc.parts.get(pid).name if self.svc.parts.get(pid) else str(pid)
            label_by_part[pid] = name
            requests.append((pid, match.line.qty, f"{stem} #{match.line.index}"))

        total_qty = sum(q for _, q, _ in requests)
        confirm = QMessageBox.question(
            self, "确认出库",
            f"将按 BOM 出库 {len(requests)} 行，合计 {total_qty} 颗。\n\n"
            f"采用先进先出，整批在一个事务里。\n任何一行库存不足则整批取消。\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        try:
            results = self.svc.stock.withdraw_many(requests)
        except BulkInsufficientStockError as exc:
            detail = "\n".join(
                f"  · {label_by_part.get(pid, pid)}：需要 {need}，现有 {have}"
                for pid, need, have in exc.shortages
            )
            QMessageBox.warning(
                self, "库存不足，整批未出库",
                f"以下 {len(exc.shortages)} 项不够，一颗都没扣：\n\n{detail}\n\n"
                f"补齐后重新执行即可。",
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("BOM 批量出库失败")
            QMessageBox.critical(self, "出库失败", str(exc))
            return

        affected = len({pid for pid, _, _ in results})
        QMessageBox.information(
            self, "出库完成",
            f"已出库 {len(results)} 行、{total_qty} 颗，涉及 {affected} 个器件。\n\n"
            f"流水备注记的是「{stem}」，在主窗口的出入库流水里能查到。",
        )
        # 出库后库存变了，整表重算一遍
        self._populate()
        self._update_summary()

    # ==================================================================
    #  未匹配行建档
    # ==================================================================

    def create_missing_parts(self) -> None:
        from app.domain.models import Part

        todo = [
            m for m in self._matches
            if m.part_id is None and not m.candidates
        ]
        if not todo:
            QMessageBox.information(
                self, "没有需要建档的行",
                "未匹配的行都已有候选器件，请先在表格里手动指定。",
            )
            return

        confirm = QMessageBox.question(
            self, "确认建档",
            f"将为 {len(todo)} 行未匹配器件建立档案：\n\n"
            f"· 库存 0，预警阈值 = BOM 需求量（这样会直接出现在「需补货」里）\n"
            f"· LCSC 编号和厂商型号写进关键词（下次匹配直接命中）\n"
            f"· 分类留空，你之后可以在主窗口里归类\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        created = skipped = 0
        for match in todo:
            line = match.line
            name = line.label
            # 同名不重复建
            exists = self.svc.db.query_one("SELECT id FROM part WHERE name = ?", (name,))
            if exists:
                skipped += 1
                continue

            # 厂商/供应商现在属于批次，但建档时还没有批次可挂 ——
            # 先塞进 keywords，下次匹配照样命中；等你真买了入库时再落到批次上。
            keywords = " ".join(
                x for x in (line.supplier_part, line.mpn, line.manufacturer,
                            line.supplier) if x
            )
            try:
                self.svc.parts.create(Part(
                    name=name,
                    category_id=None,
                    mpn=line.mpn,
                    footprint=line.footprint,
                    keywords=keywords,
                    min_stock=line.qty,
                ))
                created += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("BOM 缺料建档失败：%s", name)
                QMessageBox.warning(self, "建档失败", f"{name}：{exc}")
                break

        QMessageBox.information(
            self, "建档完成",
            f"新建 {created} 个器件" + (f"，跳过同名 {skipped} 个" if skipped else "") + "。\n\n"
            f"它们库存为 0，会出现在主窗口的「只看需补货」里。",
        )
        self._load_all_parts()
        if self._bom_path:
            self.load(self._bom_path)


# ===========================================================================
#  成本明细
# ===========================================================================

COL_C_INDEX = 0
COL_C_VALUE = 1
COL_C_PART = 2
COL_C_QTY = 3
COL_C_PRICE = 4
COL_C_SUBTOTAL = 5

_COST_HEADERS = ["行", "BOM 值 / Comment", "匹配到的器件", "数量", "单价 (¥)", "小计 (¥)"]

_RIGHT = Qt.AlignRight | Qt.AlignVCenter
_CENTER = Qt.AlignCenter


class BomCostDialog(QDialog):
    """逐行成本明细。

    **算不出钱的行置顶标色**，一眼就能看到还差什么：
        黄底  匹配到了器件，但库里没记价格 —— 可以在「单价」列直接补
        红底  库里没有对得上的器件 —— 得先解决匹配，这里改不了

    **补价写的是该器件所有在库批次**（价格本来就记在批次上，不在器件上），
    补完立即落库；关窗时用 `changed` 告诉调用方要不要重算总价。

    顺带一个"单价是快照"的坑：`CostReport` 里的单价是构造时抓的，
    改完价必须重新拉一次器件信息，光改这个对象是不会影响主界面的。
    """

    def __init__(self, services: Services, report: CostReport,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = services
        self.report = report
        self.changed = False            # 有没有真的改过价
        self._updating = False          # 防止刷新时被 itemChanged 递归打回来

        self.setWindowTitle("BOM 成本明细")
        self.resize(980, 640)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        self.lbl_head = QLabel("")
        self.lbl_head.setWordWrap(True)
        root.addWidget(self.lbl_head)

        self.table = QTableWidget(0, len(_COST_HEADERS))
        self.table.setHorizontalHeaderLabels(_COST_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(False)
        # 只有「单价」列的单元格带 ItemIsEditable，其余列双击也编辑不了 ——
        # 用单元格自带编辑而不是塞控件，整行的底色才能是连成一整条的
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
        )
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        root.addWidget(self.table, 1)

        self.lbl_total = QLabel("")
        root.addWidget(self.lbl_total)

        hint = QLabel(
            "黄底 = 匹配到了器件但库里没记价格 —— 双击「单价」列直接补"
            "（补的是该器件所有在库批次的单价，立即写库）。\n"
            "红底 = 库里没有对得上的器件，得先解决匹配（回主界面改选或建档）。"
        )
        hint.setProperty("hint", True)
        hint.setWordWrap(True)
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._populate()

    # ------------------------------------------------------------------

    def _populate(self) -> None:
        pal = theme.pal()
        self._lines: list[CostLine] = self.report.ordered

        self._updating = True
        self.table.setRowCount(len(self._lines))

        for row, line in enumerate(self._lines):
            self._put(row, COL_C_INDEX, str(line.index), align=_CENTER)
            self._put(row, COL_C_VALUE, line.label)

            if line.matched:
                self._put(row, COL_C_PART, line.part_name or "（器件已删除）")
            else:
                self._put(row, COL_C_PART, "（库里没有对得上的器件）",
                          colour=pal.fg_out)

            self._put(row, COL_C_QTY, str(line.qty), align=_RIGHT)

            # 没价的显示成 "—" 而不是 0 —— 0 在单价栏里会被读成"这颗不要钱"
            price_item = self._put(row, COL_C_PRICE,
                                   format_money(line.unit_price)
                                   if line.unit_price > 0 else "—",
                                   align=_RIGHT)
            if line.editable:
                price_item.setFlags(price_item.flags() | Qt.ItemIsEditable)
                price_item.setToolTip(
                    "双击改单价。会写到这个器件**所有在库批次**上，立即存库。"
                )
            else:
                price_item.setToolTip(
                    "这个器件没有在库批次，单价无处可写 —— 先去入库。\n"
                    "（价格记在批次上，不在器件上）"
                    if line.matched else "没有匹配到器件，无从计价。"
                )

            self._put(row, COL_C_SUBTOTAL, format_money(line.subtotal), align=_RIGHT)
            self._tint_row(row, line)

        self.table.setColumnWidth(COL_C_INDEX, 44)
        self.table.setColumnWidth(COL_C_VALUE, 170)
        self.table.setColumnWidth(COL_C_PART, 330)
        self.table.setColumnWidth(COL_C_QTY, 64)
        self.table.setColumnWidth(COL_C_PRICE, 130)
        self.table.setColumnWidth(COL_C_SUBTOTAL, 130)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.resizeRowsToContents()
        self._updating = False

        self._refresh_total()

    def _put(self, row: int, col: int, text: str,
             colour: str | None = None, align=None) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        # 默认 flags 里带 ItemIsEditable，不显式摘掉的话**每一格**都能双击进编辑
        item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        if align is not None:
            item.setTextAlignment(align)
        if colour is not None:
            item.setForeground(QColor(colour))
        self.table.setItem(row, col, item)
        return item

    def _tint_row(self, row: int, line: CostLine) -> None:
        """整行底色：黄 = 缺价，红 = 没匹配上，正常行不着色。"""
        pal = theme.pal()
        if line.missing_price:
            tint = QColor(pal.bg_low)
        elif not line.matched:
            tint = QColor(pal.bg_out)
        else:
            tint = None

        for col in range(self.table.columnCount()):
            item = self.table.item(row, col)
            if item is not None:
                item.setBackground(tint if tint is not None else QColor(Qt.transparent))

    def _refresh_total(self) -> None:
        pal = theme.pal()
        total = sum(l.subtotal for l in self._lines)
        missing = [l for l in self._lines if l.missing_price]
        unmatched = [l for l in self._lines if not l.matched]

        colour = pal.fg_ok if not missing and not unmatched else pal.fg_attention
        bits = [f"合计 ¥{format_money_total(total)}"]
        if missing:
            bits.append(f"{len(missing)} 项还没价格")
        if unmatched:
            bits.append(f"{len(unmatched)} 行没匹配上")
        if not missing and not unmatched:
            bits.append("全都算得出来")
        self.lbl_total.setText("　·　".join(bits))
        self.lbl_total.setStyleSheet(f"color: {colour}; font-weight: 500;")

        self.lbl_head.setText(
            f"共 {len(self._lines)} 行。算不出钱的行已经置顶标色 —— "
            f"黄的补个价就行（改完立即存库），红的得先解决匹配。"
        )

    # ------------------------------------------------------------------

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != COL_C_PRICE:
            return
        row = item.row()
        if row >= len(self._lines):
            return
        line = self._lines[row]

        text = item.text().strip()
        if text in ("", "—"):
            # 清空 / 没动：不算改价，原样画回去
            self._refresh_row(row)
            return
        try:
            value = float(text) if text else 0.0
        except ValueError:
            QMessageBox.warning(self, "单价格式不对", f"「{text}」不是有效数字。")
            self._refresh_row(row)
            return
        if value < 0:
            QMessageBox.warning(self, "单价不能是负数", f"「{text}」填成负数了。")
            self._refresh_row(row)
            return

        if not line.editable:
            QMessageBox.information(
                self, "改不了", "这个器件没有在库批次，单价无处可写 —— 先去入库。"
            )
            self._refresh_row(row)
            return

        try:
            touched = self.svc.stock.set_unit_price(line.part_id, value)
        except Exception as exc:  # noqa: BLE001
            log.exception("BOM 成本明细改价失败")
            QMessageBox.critical(self, "改价失败", str(exc))
            self._refresh_row(row)
            return

        if touched <= 0:
            QMessageBox.warning(
                self, "没改成",
                "这个器件没有数量大于 0 的批次，单价没有落点。先去入库。",
            )
            self._refresh_row(row)
            return

        line.unit_price = value
        self.changed = True
        self._refresh_row(row)
        self._refresh_total()

    def _refresh_row(self, row: int) -> None:
        """把一行的单价 / 小计 / 底色按最新状态重画。"""
        line = self._lines[row]
        self._updating = True
        price_item = self.table.item(row, COL_C_PRICE)
        if price_item is not None:
            price_item.setText(format_money(line.unit_price))
        sub_item = self.table.item(row, COL_C_SUBTOTAL)
        if sub_item is not None:
            sub_item.setText(format_money(line.subtotal))
        self._tint_row(row, line)
        self._updating = False
