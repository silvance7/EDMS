"""主管理窗口：分类树 + 器件表格 + 详情面板。

三栏用 QSplitter 分隔，中间表格占大头。窗口关闭时**不退出程序**，
而是收进托盘（除非用户明确选了退出）——见 main.py 的处理。

数据流一律是：界面动作 -> Services -> 重新查询 -> 刷新模型。
不做本地缓存、不做增量修补，因为库很小、查询很快，
"每次都从库里重新读"永远比"猜哪些内存状态需要更新"更不容易出错。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.domain import Services
from app.domain.models import SearchQuery
from app.ui import format_money, theme
from app.ui.part_editor import PartEditorDialog, build_category_labels
from app.ui.part_table_model import COLUMNS, PartTableModel
from app.ui.stock_dialog import StockInDialog, StockOutDialog

log = logging.getLogger("edms.ui.main_window")

ROLE_ID = Qt.UserRole + 1


class MainWindow(QMainWindow):
    # 通知外部（托盘）状态变了，需要更新提示文字
    status_changed = Signal(str)
    # 关闭窗口被拦下、收进托盘时发出，让托盘弹个气泡告诉用户程序还活着
    hidden_to_tray = Signal(str)

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = services
        self._selected_part_id: int | None = None

        # 标题带个后缀，避免和文件资源管理器的文件夹名撞车
        # （自动化截图工具按标题子串找窗口时会挑错）
        self.setWindowTitle("元器件管理 · 本地库存")
        self._resize_to_fit()

        # 搜索防抖：打字时不要每敲一个字符就查一次库
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._reload_table)

        self._build_ui()
        self._build_shortcuts()
        self.reload_all()

    # ==================================================================
    #  界面搭建
    # ==================================================================

    def _resize_to_fit(self) -> None:
        """按屏幕可用区域定尺寸。

        写死 1360x800 在带缩放的笔记本上（125% 很常见）会超出屏幕，
        右边的详情面板直接被顶到屏幕外，用户根本看不到。
        所以取可用区域的 92%，并给一个下限防止窗口小到不能用。
        """
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1360, 800)
            return

        geo = screen.availableGeometry()
        width = max(1080, min(1400, int(geo.width() * 0.92)))
        height = max(620, min(880, int(geo.height() * 0.92)))
        self.resize(width, height)
        self.move(
            geo.x() + (geo.width() - width) // 2,
            geo.y() + (geo.height() - height) // 2,
        )

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 6)
        outer.setSpacing(8)

        outer.addWidget(self._build_toolbar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_category_pane())
        splitter.addWidget(self._build_table_pane())
        splitter.addWidget(self._build_detail_pane())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([210, 760, 340])
        outer.addWidget(splitter, 1)

        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)

    # ---------------- 顶部工具条 ----------------

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索：10k 0603 / RC0603 / 电阻  ← 多个词用空格，全部命中才显示")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(lambda _: self._search_timer.start())
        h.addWidget(self.ed_search, 1)

        self.cb_footprint = QComboBox()
        self.cb_footprint.setMinimumWidth(110)
        self.cb_footprint.currentIndexChanged.connect(self._reload_table)
        h.addWidget(self.cb_footprint)

        self.chk_low = QCheckBox("只看需补货")
        self.chk_low.setToolTip("显示库存为零、或低于预警阈值的器件")
        self.chk_low.stateChanged.connect(self._reload_table)
        h.addWidget(self.chk_low)

        self.btn_bom = QPushButton("BOM 操作")
        self.btn_bom.setToolTip(
            "打开 BOM 操作中心（三个页签）：\n"
            "· 比对 —— 载入 KiCad 导出的 BOM（.xlsx / .csv），对照库存逐行匹配，\n"
            "  之后一键全出库，缺料的行还能一键建档\n"
            "· 导出 —— 把当前库存的全部器件导成一份 CSV 清单\n"
            "· 导入 —— 从清单文件批量导入器件（没有的建档，带数量的按批次入库）"
        )
        self.btn_bom.clicked.connect(self.open_bom_dialog)
        h.addWidget(self.btn_bom)

        # 这里**故意不放「新建器件」按钮** —— 建器件的正路是走入库面板
        # （搜不到就现场建）。纯建档（库存 0）的需求放在表格右键菜单里，不占地方。

        return bar

    # ---------------- 左：分类树 ----------------

    def _build_category_pane(self) -> QWidget:
        box = QGroupBox("分类")
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 6, 6, 6)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.tree.itemSelectionChanged.connect(self._reload_table)
        v.addWidget(self.tree, 1)

        return box

    # ---------------- 中：器件表格 ----------------

    def _build_table_pane(self) -> QWidget:
        box = QGroupBox("器件清单")
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)

        self.model = PartTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.doubleClicked.connect(lambda _: self.edit_selected_part())
        self.table.selectionModel().selectionChanged.connect(self._on_row_selected)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)

        header = self.table.horizontalHeader()
        for i, col in enumerate(COLUMNS):
            self.table.setColumnWidth(i, col.width)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setSortIndicatorShown(False)

        v.addWidget(self.table, 1)

        # 底部动作条
        actions = QWidget()
        ah = QHBoxLayout(actions)
        ah.setContentsMargins(0, 0, 0, 0)
        ah.setSpacing(8)

        self.btn_in = QPushButton("入库 +")
        self.btn_in.clicked.connect(lambda: self.stock_in())
        ah.addWidget(self.btn_in)

        self.btn_out = QPushButton("出库 −")
        self.btn_out.clicked.connect(lambda: self.stock_out())
        ah.addWidget(self.btn_out)

        self.btn_edit = QPushButton("编辑")
        self.btn_edit.clicked.connect(self.edit_selected_part)
        ah.addWidget(self.btn_edit)

        self.btn_del = QPushButton("删除")
        self.btn_del.setProperty("danger", True)
        self.btn_del.clicked.connect(self.delete_selected_part)
        ah.addWidget(self.btn_del)

        ah.addStretch(1)
        v.addWidget(actions)

        return box

    # ---------------- 右：详情面板 ----------------

    def _build_detail_pane(self) -> QWidget:
        box = QGroupBox("详情")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        self.lbl_title = QLabel("未选中器件")
        self.lbl_title.setProperty("title", True)
        self.lbl_title.setWordWrap(True)
        v.addWidget(self.lbl_title)

        self.lbl_sub = QLabel("")
        self.lbl_sub.setProperty("hint", True)
        self.lbl_sub.setWordWrap(True)
        v.addWidget(self.lbl_sub)

        self.lbl_state = QLabel("")
        v.addWidget(self.lbl_state)

        self.lbl_stock = QLabel("")
        self.lbl_stock.setWordWrap(True)
        v.addWidget(self.lbl_stock)

        v.addWidget(QLabel("规格参数"))
        self.tbl_params = QTableWidget(0, 2)
        self.tbl_params.horizontalHeader().setVisible(False)
        self.tbl_params.verticalHeader().setVisible(False)
        self.tbl_params.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_params.setSelectionMode(QAbstractItemView.NoSelection)
        self.tbl_params.setMaximumHeight(150)
        v.addWidget(self.tbl_params)

        v.addWidget(QLabel("库存批次（右键可改：价格 / 数量 / 删除）"))
        self.tbl_lots = QTableWidget(0, 5)
        self.tbl_lots.setHorizontalHeaderLabels(
            ["位置", "数量", "单价", "购买日期", "厂商 / 供应商"])
        self.tbl_lots.verticalHeader().setVisible(False)
        self.tbl_lots.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # 之前是 NoSelection —— 那样右键都不知道点的是哪一行，改成单选
        self.tbl_lots.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_lots.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_lots.setMaximumHeight(160)
        self.tbl_lots.horizontalHeader().setStretchLastSection(True)
        self.tbl_lots.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl_lots.customContextMenuRequested.connect(self._show_lot_menu)
        v.addWidget(self.tbl_lots)

        v.addWidget(QLabel("出入库流水"))
        self.txt_logs = QPlainTextEdit()
        self.txt_logs.setReadOnly(True)
        self.txt_logs.setFixedHeight(110)
        v.addWidget(self.txt_logs)

        v.addStretch(1)
        return box

    # ---------------- 快捷键 ----------------

    def _build_shortcuts(self) -> None:
        new_act = QAction(self)
        new_act.setShortcut(QKeySequence("Ctrl+N"))
        new_act.triggered.connect(self.new_part)
        self.addAction(new_act)

        focus_act = QAction(self)
        focus_act.setShortcut(QKeySequence("Ctrl+F"))
        focus_act.triggered.connect(lambda: (self.ed_search.setFocus(), self.ed_search.selectAll()))
        self.addAction(focus_act)

        del_act = QAction(self)
        del_act.setShortcut(QKeySequence("Delete"))
        del_act.triggered.connect(self.delete_selected_part)
        self.addAction(del_act)

        esc_act = QAction(self)
        esc_act.setShortcut(QKeySequence("Esc"))
        esc_act.triggered.connect(self._clear_filters)
        self.addAction(esc_act)

    # ==================================================================
    #  数据刷新
    # ==================================================================

    def reload_all(self) -> None:
        self._reload_categories()
        self._reload_footprints()
        self._reload_table()

    def _reload_categories(self) -> None:
        """重建分类树，尽量保住当前选中项。"""
        previous = self._current_category_id()

        self.tree.blockSignals(True)
        self.tree.clear()

        root = QTreeWidgetItem(["全部器件"])
        root.setData(0, ROLE_ID, None)
        self.tree.addTopLevelItem(root)

        by_parent: dict[int | None, list] = {}
        for cat in self.svc.tree.list_categories():
            by_parent.setdefault(cat.parent_id, []).append(cat)

        def add_children(parent_item: QTreeWidgetItem, parent_id: int | None) -> int:
            count = 0
            for cat in by_parent.get(parent_id, []):
                node = QTreeWidgetItem([cat.name])
                node.setData(0, ROLE_ID, cat.id)
                parent_item.addChild(node)
                count += 1 + add_children(node, cat.id)
            return count

        add_children(root, None)
        self.tree.expandAll()
        self.tree.blockSignals(False)

        # 还原选中
        target = root
        if previous is not None:
            it = self._find_tree_item(root, previous)
            if it is not None:
                target = it
        self.tree.setCurrentItem(target)

    def _find_tree_item(self, parent: QTreeWidgetItem, cat_id: int) -> QTreeWidgetItem | None:
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child.data(0, ROLE_ID) == cat_id:
                return child
            found = self._find_tree_item(child, cat_id)
            if found is not None:
                return found
        return None

    def _current_category_id(self) -> int | None:
        item = self.tree.currentItem()
        if item is None:
            return self._last_category_id if hasattr(self, "_last_category_id") else None
        return item.data(0, ROLE_ID)

    def _reload_footprints(self) -> None:
        self.cb_footprint.blockSignals(True)
        current = self.cb_footprint.currentText()
        self.cb_footprint.clear()
        self.cb_footprint.addItem("全部封装", "")
        for fp in self.svc.search.footprints():
            self.cb_footprint.addItem(fp, fp)
        idx = self.cb_footprint.findData(current)
        self.cb_footprint.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_footprint.blockSignals(False)

    def _build_query(self) -> SearchQuery:
        item = self.tree.currentItem()
        cat_id = item.data(0, ROLE_ID) if item is not None else None
        self._last_category_id = cat_id

        return SearchQuery(
            keyword=self.ed_search.text().strip(),
            category_id=cat_id,
            footprint=self.cb_footprint.currentData() or "",
            only_low_stock=self.chk_low.isChecked(),
            limit=2000,
        )

    def _reload_table(self) -> None:
        query = self._build_query()
        rows = self.svc.search.search(query)

        # 保住当前选中的器件，避免刷新后跳走
        keep = self._selected_part_id
        self.model.set_rows(rows)

        target_row = -1
        if keep is not None:
            for i, row in enumerate(rows):
                if row.id == keep:
                    target_row = i
                    break

        if target_row >= 0:
            self.table.selectRow(target_row)
        else:
            self.table.clearSelection()
            if self._selected_part_id is not None:
                self._selected_part_id = None
                self._clear_detail()

        total_kinds = len(rows)
        total_qty = sum(r.total_qty for r in rows)
        low = sum(1 for r in rows if r.is_low_stock or r.is_out_of_stock)
        self.status_label.setText(
            f"共 {total_kinds} 种器件 · 合计 {total_qty} 颗 · "
            f"库存总货值 ¥{self.svc.stock.inventory_value():,.2f}"
            + (f" · <span style='color:{theme.pal().fg_out}'>{low} 种需补货</span>" if low else "")
        )
        self.status_label.setTextFormat(Qt.RichText)
        self.status_changed.emit(f"{total_kinds} 种器件，{low} 种需补货")

    # ==================================================================
    #  选中与详情
    # ==================================================================

    def _on_row_selected(self, *_args) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.model.row_at(rows[0].row())
        if item is None:
            return
        self._selected_part_id = item.id
        self._show_detail(item.id)

    def _clear_detail(self) -> None:
        self.lbl_title.setText("未选中器件")
        self.lbl_sub.setText("")
        self.lbl_state.setText("")
        self.lbl_stock.setText("")
        self.tbl_params.setRowCount(0)
        self.tbl_lots.setRowCount(0)
        self.txt_logs.setPlainText("")

    def _show_detail(self, part_id: int) -> None:
        part = self.svc.parts.get(part_id)
        if part is None:
            self._clear_detail()
            return

        self.lbl_title.setText(part.name)

        total = self.svc.stock.total_quantity(part_id)
        lots = self.svc.stock.lots(part_id)
        value = sum(l.quantity * l.unit_price for l in lots)

        bits = []
        # 厂商来自批次聚合：同一个器件可能有好几家的货（YAGEO 的 + 厚声的）
        makers = sorted({lot.manufacturer for lot in lots if lot.manufacturer})
        if makers:
            bits.append(" / ".join(makers))
        if part.mpn:
            bits.append(part.mpn)
        if part.footprint:
            bits.append(f"封装 {part.footprint}")
        self.lbl_sub.setText("　·　".join(bits) or "—")

        if total <= 0:
            self.lbl_state.setText("⊘ 已缺货")
            self.lbl_state.setProperty("state", "out")
        elif part.min_stock > 0 and total <= part.min_stock:
            self.lbl_state.setText(f"! 库存偏低（阈值 {part.min_stock}）")
            self.lbl_state.setProperty("state", "low")
        else:
            self.lbl_state.setText("库存正常")
            self.lbl_state.setProperty("state", "")
        # 属性变了要重新应用样式表才生效
        self.lbl_state.style().unpolish(self.lbl_state)
        self.lbl_state.style().polish(self.lbl_state)

        self.lbl_stock.setText(
            f"总库存 <b>{total}</b>　批次 {len(lots)}　货值 ¥{value:,.2f}"
        )

        # --- 规格参数 ---
        params = self.svc.parts.get_params(part_id)
        self.tbl_params.setRowCount(len(params))
        for r, pv in enumerate(params):
            self.tbl_params.setItem(r, 0, QTableWidgetItem(pv.name))
            self.tbl_params.setItem(r, 1, QTableWidgetItem(pv.display))
        self.tbl_params.resizeColumnsToContents()
        if self.tbl_params.columnCount() == 2:
            self.tbl_params.setColumnWidth(0, max(80, self.tbl_params.columnWidth(0)))

        # --- 批次 ---
        self.tbl_lots.setRowCount(len(lots))
        self._lot_ids = [lot.id for lot in lots]
        for r, lot in enumerate(lots):
            self.tbl_lots.setItem(r, 0, QTableWidgetItem(lot.location_label or "未指定"))
            qty_item = QTableWidgetItem(str(lot.quantity))
            self.tbl_lots.setItem(r, 1, qty_item)
            self.tbl_lots.setItem(
                r, 2, QTableWidgetItem(format_money(lot.unit_price) if lot.unit_price else "—"))
            self.tbl_lots.setItem(r, 3, QTableWidgetItem(lot.purchase_date or "—"))
            source = " / ".join(x for x in (lot.manufacturer, lot.supplier) if x)
            self.tbl_lots.setItem(r, 4, QTableWidgetItem(source or "—"))
        self.tbl_lots.resizeColumnsToContents()

        # --- 流水 ---
        logs = self.svc.stock.logs(part_id, limit=40)
        lines = []
        for lg in logs:
            sign = "+" if lg.delta > 0 else ""
            lines.append(
                f"{lg.created_at[5:16]}  {sign}{lg.delta:>6}  {lg.reason}"
                + (f"  {lg.ref}" if lg.ref else "")
            )
        self.txt_logs.setPlainText("\n".join(lines) if lines else "（暂无流水）")

    # ==================================================================
    #  动作
    # ==================================================================

    # ------------------------------------------------------------------
    #  批次的右键修正（记错了价格 / 数量 / 日期的时候用）
    # ------------------------------------------------------------------

    def _show_lot_menu(self, pos) -> None:
        """批次表右键：改价、盘点改数量、改日期、删批次。

        批次是入库时**自动**生成的，用户不用记 —— 但人总会填错：
        单价多敲一个零、数量多数一盘、日期写错。之前这些字段服务层
        都能改，界面上却一个入口都没有，错了只能去数据库里动手。
        """
        row = self.tbl_lots.rowAt(pos.y())
        if row < 0 or row >= len(getattr(self, "_lot_ids", [])):
            return
        self.tbl_lots.selectRow(row)
        lot_id = self._lot_ids[row]
        lot = self.svc.stock.lot(lot_id)
        if lot is None:
            return

        menu = QMenu(self)
        act_price = menu.addAction(f"改单价…（现在 ¥{format_money(lot.unit_price)}）")
        act_qty = menu.addAction(f"盘点数量…（现在 {lot.quantity}）")
        act_date = menu.addAction(f"改购买日期…（现在 {lot.purchase_date or '未填'}）")
        menu.addSeparator()
        act_del = menu.addAction("删除这一批次")
        chosen = self._exec_lot_menu(menu, pos)

        if chosen is act_price:
            text, ok = QInputDialog.getText(
                self, "改单价", "单颗价格（元）：", text=str(lot.unit_price or 0))
            if not ok:
                return
            try:
                value = float(text.strip())
            except ValueError:
                QMessageBox.warning(self, "格式不对", f"「{text}」不是有效数字。")
                return
            if value < 0:
                QMessageBox.warning(self, "单价不能是负数", "改个小点的数。")
                return
            self.svc.stock.update_lot(lot_id, unit_price=value)
        elif chosen is act_qty:
            qty, ok = QInputDialog.getInt(
                self, "盘点数量",
                "实际清点出来的数量（会记一条盘点流水）：",
                lot.quantity, 0, 10_000_000)
            if not ok:
                return
            self.svc.stock.adjust(lot_id, qty, ref="手动盘点修正")
        elif chosen is act_date:
            text, ok = QInputDialog.getText(
                self, "改购买日期", "格式 yyyy-MM-dd，可留空：",
                text=lot.purchase_date or "")
            if not ok:
                return
            self.svc.stock.update_lot(lot_id, purchase_date=text.strip())
        elif chosen is act_del:
            if QMessageBox.question(
                self, "删除批次",
                f"删除批次 #{lot_id}（{lot.quantity} 颗 @ ¥{format_money(lot.unit_price)}）？\n\n"
                f"出入库流水会保留，只是这条批次没了。\n"
                f"如果只是数量记错，用「盘点数量」更合适 —— 它会留痕。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            self.svc.stock.delete_lot(lot_id)
        else:
            return

        # 详情面板要跟到**被改的那个批次所属的器件**。
        # 不能用表格当前的选中 —— 表格选中会随筛选变，改的可能根本不是它。
        self._selected_part_id = lot.part_id
        self._reload_table()            # 里头会把表格选中跟过去
        self._show_detail(lot.part_id)  # 就算这行被筛选掉了，详情也得是对的

    def _exec_lot_menu(self, menu: QMenu, pos) -> None:
        """弹出菜单并等用户选。单独拆出来是为了测试能把模态循环替掉 ——
        QMenu.exec 是静态方法，直接替换类属性在 PySide6 里不生效。"""
        return menu.exec(self.tbl_lots.viewport().mapToGlobal(pos))

    def new_part(self) -> None:
        dlg = PartEditorDialog(
            self.svc.parts,
            build_category_labels(self.svc.tree),
            default_category_id=self._current_category_id(),
            parent=self,
        )
        if dlg.exec():
            self._selected_part_id = dlg.saved_part_id
            self.reload_all()

    def open_bom_dialog(self) -> None:
        """打开 BOM 操作对话框（比对 / 导出 / 导入三个页签）。

        import 放在方法里是有意的：BOM 模块只在真正用到时才加载，
        不给启动路径添负担（和主窗口、浮窗的懒加载是同一个思路）。

        关掉之后整体刷新 —— 里面可能出了库（库存变了），
        也可能建了档 / 导入了器件（器件变多了）。
        """
        from app.ui.bom_dialog import BomDialog

        dlg = BomDialog(self.svc, self)
        dlg.exec()
        self.reload_all()
        if self._selected_part_id is not None:
            self._show_detail(self._selected_part_id)

    def edit_selected_part(self) -> None:
        if self._selected_part_id is None:
            QMessageBox.information(self, "没有选中器件", "请先在列表里选一个器件。")
            return
        dlg = PartEditorDialog(
            self.svc.parts,
            build_category_labels(self.svc.tree),
            part_id=self._selected_part_id,
            parent=self,
        )
        if dlg.exec():
            self.reload_all()
            self._show_detail(self._selected_part_id)

    def delete_selected_part(self) -> None:
        if self._selected_part_id is None or not self.table.selectionModel().selectedRows():
            return
        part = self.svc.parts.get(self._selected_part_id)
        if part is None:
            return

        total = self.svc.stock.total_quantity(part.id)
        detail = f"\n\n它还有 {total} 颗库存分布在 {len(self.svc.stock.lots(part.id))} 个批次里，" \
                 f"连同全部出入库流水都会被一起删除。" if total else ""

        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除「{part.name}」吗？{detail}\n\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.svc.parts.delete(part.id)
        self._selected_part_id = None
        self.reload_all()
        self._clear_detail()

    def stock_in(self, preset_part_id: int | None = None) -> None:
        """打开入库面板。

        **不要求先在表格里选中器件** —— 面板顶部就是搜索框：搜到就选，
        搜不到就在同一个面板里现场新建。如果表格里正好选中了某行，
        就把它带进去当默认值，省一步。
        """
        if preset_part_id is None and self.table.selectionModel().hasSelection():
            preset_part_id = self._selected_part_id

        dlg = StockInDialog(self.svc, self, preset_part_id=preset_part_id)
        if dlg.exec():
            self._selected_part_id = dlg.part_id
            self.reload_all()
            if dlg.part_id is not None:
                self._show_detail(dlg.part_id)

    def stock_out(self, preset_part_id: int | None = None) -> None:
        """打开出库面板。同样不要求先选中 —— 面板里能搜、能一次加多个器件。"""
        if preset_part_id is None and self.table.selectionModel().hasSelection():
            preset_part_id = self._selected_part_id

        dlg = StockOutDialog(self.svc, self, preset_part_id=preset_part_id)
        if dlg.exec():
            self.reload_all()
            if self._selected_part_id is not None:
                self._show_detail(self._selected_part_id)

    def _require_selection(self) -> int | None:
        if self._selected_part_id is None:
            QMessageBox.information(self, "没有选中器件", "请先在列表里选一个器件。")
            return None
        return self._selected_part_id

    def _clear_filters(self) -> None:
        self.ed_search.clear()
        self.chk_low.setChecked(False)
        if self.cb_footprint.count():
            self.cb_footprint.setCurrentIndex(0)
        root = self.tree.topLevelItem(0)
        if root is not None:
            self.tree.setCurrentItem(root)

    def _on_table_context_menu(self, pos) -> None:
        """表格右键菜单。

        「新建器件」放在这里而不是工具条上：建器件的**正路是走入库面板**
        （搜不到就现场建），只有"先建档、以后再补货"这种少见情况才需要
        单独建一条空器件，右键菜单里正好，不占工具条的视觉重量。
        """
        has_row = self.table.selectionModel().hasSelection()

        menu = QMenu(self)
        act_in = menu.addAction("入库…")
        act_out = menu.addAction("出库…")
        menu.addSeparator()
        act_edit = menu.addAction("编辑器件…")
        act_del = menu.addAction("删除器件")
        menu.addSeparator()
        act_new = menu.addAction("新建器件（纯建档，库存 0）")

        for act in (act_in, act_out, act_edit, act_del):
            act.setEnabled(has_row)

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_in:
            self.stock_in()
        elif chosen is act_out:
            self.stock_out()
        elif chosen is act_edit:
            self.edit_selected_part()
        elif chosen is act_del:
            self.delete_selected_part()
        elif chosen is act_new:
            self.new_part()

    # ==================================================================
    #  分类右键菜单
    # ==================================================================

    def _on_tree_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        cat_id = item.data(0, ROLE_ID)

        menu = QMenu(self)
        act_new_sub = menu.addAction("新建子分类")
        if cat_id is not None:
            act_add_param = menu.addAction("为该分类添加参数…")
            menu.addSeparator()
            act_rename = menu.addAction("重命名")
            act_delete = menu.addAction("删除分类")
        else:
            act_add_param = act_rename = act_delete = None

        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen is act_new_sub:
            self._create_subcategory(cat_id)
        elif act_add_param is not None and chosen is act_add_param:
            self._add_param_template(cat_id)
        elif act_rename is not None and chosen is act_rename:
            self._rename_category(cat_id, item.text(0))
        elif act_delete is not None and chosen is act_delete:
            self._delete_category(cat_id, item.text(0))

    def _create_subcategory(self, parent_id: int | None) -> None:
        name, ok = QInputDialog.getText(self, "新建分类", "分类名称：")
        if not ok or not name.strip():
            return
        try:
            self.svc.tree.create_category(name.strip(), parent_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("新建分类失败")
            QMessageBox.warning(self, "无法创建", f"同层级下可能已有同名分类。\n\n{exc}")
            return
        self._reload_categories()

    def _rename_category(self, cat_id: int, old_name: str) -> None:
        name, ok = QInputDialog.getText(self, "重命名分类", "新名称：", text=old_name)
        if not ok or not name.strip():
            return
        self.svc.tree.rename_category(cat_id, name.strip())
        self._reload_categories()
        # 分类名进了检索列，改名后要重建
        self.svc.parts.rebuild_all_search_text()
        self._reload_table()

    def _delete_category(self, cat_id: int, name: str) -> None:
        sub = len(self.svc.tree.category_ids_with_children(cat_id)) - 1
        msg = f"确定删除分类「{name}」吗？"
        if sub:
            msg += f"\n\n它的 {sub} 个子分类也会一起删除。"
        msg += "\n\n该分类下的器件**不会**被删除，只是变成「未分类」。"
        if QMessageBox.question(self, "确认删除", msg,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.svc.tree.delete_category(cat_id)
        self._reload_categories()
        self._reload_table()

    def _add_param_template(self, cat_id: int) -> None:
        name, ok = QInputDialog.getText(self, "添加参数", "参数名称（如：阻值）：")
        if not ok or not name.strip():
            return
        unit, ok = QInputDialog.getText(self, "添加参数", "单位（如 Ω，可留空）：")
        if not ok:
            return
        dtype, ok = QInputDialog.getItem(
            self, "添加参数", "类型：", ["数值", "文本"], 0, False
        )
        if not ok:
            return
        try:
            self.svc.parts.add_template(
                cat_id, name.strip(), unit.strip(), "num" if dtype == "数值" else "text"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("添加参数模板失败")
            QMessageBox.warning(self, "无法添加", f"该分类下可能已有同名参数。\n\n{exc}")
            return

        new_count = self.svc.parts.rebuild_all_search_text()
        QMessageBox.information(
            self, "已添加",
            f"参数「{name.strip()}」已加到该分类。\n"
            f"它的子分类会自动继承这个参数。\n\n"
            f"已重建 {new_count} 个器件的检索索引。",
        )

    # ==================================================================
    #  外部调用
    # ==================================================================

    def focus_search(self) -> None:
        """被托盘或全局热键唤起时调用：把窗口带到前台并聚焦搜索框。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.ed_search.setFocus()
        self.ed_search.selectAll()

    def refresh(self) -> None:
        self.reload_all()
        if self._selected_part_id is not None:
            self._show_detail(self._selected_part_id)

    # ==================================================================
    #  关闭行为
    # ==================================================================

    def closeEvent(self, event) -> None:  # noqa: N802
        """点右上角的 × 只是收进托盘，**不**退出程序。

        这是"常驻"这个形态的前提：用户以为关掉了，其实热键随时还能用。
        真正退出走托盘菜单里的「退出」。
        """
        if getattr(self, "_force_close", False):
            event.accept()
            return
        event.ignore()
        self.hide()
        self.hidden_to_tray.emit(f"仍在后台运行")
