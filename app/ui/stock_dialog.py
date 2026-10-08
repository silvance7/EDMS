"""入库 / 出库面板。

**入库流程（2026-10-08 简化）**：点「入库 +」直接开面板，顶部就是器件搜索：

    ① 选器件   —— 搜到就选中；搜不到，点「＋ 新建器件」打开完整器件表单
    ② 入库资料 —— 数量 / 位置 / 单价 / 日期 / 厂商 / 供应商 / 备注

「＋ 新建器件」用的是**完整器件表单**（分类 / 型号 / 封装 / 参数 / 关键词
都能填），和主窗口、设置面板「分类管理」里是同一套逻辑，建完自动选中回来。
原先只收 4 个字段的内嵌小表单已删除 —— 它就是"误建无属性器件"的来源。

⚠️ **回车不提交**：本面板继承 EnterSafeDialog —— QDialog 的默认按钮会被
任何输入框的回车触发，这里为此出过事故（根因与实验结论见 dialog_base.py）。

**厂商记在批次上，不在器件上。** 同一个 10kΩ 电阻，YAGEO 买的和厚声买的是
同一条器件记录，只是两条采购批次 —— 换厂牌不该逼你新建一个器件。

**出库**支持一次出多个器件，每行可以指定从哪个批次扣（默认先进先出）。
出库前按「全有才出」预检：任何一行库存不足就整批取消，一颗都不扣。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.domain import Services
from app.domain.models import SearchQuery
from app.domain.stock_service import BulkInsufficientStockError
from app.domain.tree_service import TreeService
from app.ui import format_money, format_money_total, theme
from app.ui.dialog_base import EnterSafeDialog
from app.ui.part_editor import PartEditorDialog, build_category_labels

log = logging.getLogger("edms.ui.stock_dialog")

ROLE_PART_ID = Qt.UserRole + 1


def _today() -> str:
    return QDate.currentDate().toString("yyyy-MM-dd")


# 报价单位：(显示文字, 一颗=多少倍)。填进去的值 / 倍率 = 单颗价格。
# 只影响"怎么填"，不影响"怎么存" —— 库里永远是单颗价格。
PRICE_UNITS = [("元/颗", 1), ("元/百颗", 100), ("元/千颗", 1000)]


def fill_location_combo(combo: QComboBox, tree: TreeService,
                        include_unspecified: bool = True) -> None:
    """往位置下拉里填项。显示完整路径（元件柜B / B3-模块），数据存的是 id。

    单独拆出来是因为改完位置要**原地重填**（保住当前选中），
    重建一个新 combo 的话，外面那行布局还得跟着换。
    """
    combo.clear()
    if include_unspecified:
        combo.addItem("（未指定）", None)
    for loc in tree.list_locations():
        combo.addItem(tree.location_path(loc.id) or loc.name, loc.id)


def build_location_combo(tree: TreeService, include_unspecified: bool = True) -> QComboBox:
    combo = QComboBox()
    fill_location_combo(combo, tree, include_unspecified)
    return combo


def combo_row(combo: QComboBox, on_manage) -> QWidget:
    """下拉框 + 右侧一个「…」设置按钮。

    下拉框占的宽度被压短，省出来的位置给设置入口 ——
    没有这个入口，想加个新抽屉就得退出去主窗口绕一圈再回来。
    `on_manage` 是无参回调；按钮用 lambda 包一层，避免 clicked 的 bool
    被当成参数塞进去（见 StockOutDialog.add_part 那个坑）。
    """
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(4)
    h.addWidget(combo, 1)

    btn = QPushButton("…")
    btn.setFixedWidth(30)
    btn.setToolTip("管理：新增 / 重命名 / 删除")
    btn.clicked.connect(lambda: on_manage())
    h.addWidget(btn)
    return row


# ===========================================================================
#  器件搜索控件
# ===========================================================================

class PartSearch(QWidget):
    """输入 -> 实时出结果 -> 选中。入库面板和「加器件」选择器共用。

    **不做"输入的名字自动建档"**：搜不到就是搜不到，建档走显式的
    「＋ 新建器件」按钮（在入库面板上，打开完整器件表单）。以前搜不到会在
    列表里塞一行「＋ 新建器件「关键词」」并自动高亮 —— 回车确认时，**同一个
    按键事件**还会继续冒泡去触发对话框的默认按钮，「入库」当场落库：
    光标还停在名称框里，器件已经带着默认数量 1 建好了（误建事故的源头）。

    `empty_hint` 是"没搜到"时给用户的下一步提示（入库面板与选择器场景不同）。
    """

    picked = Signal(int)

    def __init__(self, services: Services, parent: QWidget | None = None,
                 empty_hint: str = "没找到") -> None:
        super().__init__(parent)
        self.svc = services
        self.empty_hint = empty_hint

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜器件：名称 / 型号 / 阻值 / 封装……  例如 10k 0603")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(lambda _: self._timer.start())
        self.ed_search.returnPressed.connect(self._pick_current)
        root.addWidget(self.ed_search)

        self.list = QTreeWidget()
        self.list.setColumnCount(2)
        self.list.setHeaderLabels(["器件", "现有库存"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setMinimumHeight(150)
        self.list.itemClicked.connect(self._on_item)
        self.list.itemActivated.connect(self._on_item)
        self.list.setColumnWidth(0, 340)
        self.list.header().setStretchLastSection(True)
        root.addWidget(self.list)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setProperty("hint", True)
        root.addWidget(self.lbl_hint)

        # 防抖：打字过程中别每敲一个字符就查一次库
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.refresh)

        self.refresh()

    # ------------------------------------------------------------------

    def keyword(self) -> str:
        return self.ed_search.text().strip()

    def focus(self) -> None:
        self.ed_search.setFocus()
        self.ed_search.selectAll()

    def refresh(self) -> None:
        keyword = self.keyword()
        self.list.clear()

        rows = self.svc.search.search(SearchQuery(keyword=keyword, limit=40))
        palette = theme.pal()

        for overview in rows:
            node = QTreeWidgetItem([overview.name, str(overview.total_qty)])
            node.setData(0, ROLE_PART_ID, overview.id)
            node.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            node.setToolTip(0, (
                f"<b>{overview.name}</b><br>"
                f"封装：{overview.footprint or '—'}<br>"
                f"型号：{overview.mpn or '—'}<br>"
                f"分类：{overview.category_path or '—'}<br>"
                f"存放：{overview.location_summary}"
            ))
            if overview.is_out_of_stock:
                node.setForeground(1, QColor(palette.fg_out))
            elif overview.is_low_stock:
                node.setForeground(1, QColor(palette.fg_low))
            self.list.addTopLevelItem(node)

        if self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0))

        if rows:
            self.lbl_hint.setText(f"{len(rows)} 个匹配")
        else:
            self.lbl_hint.setText(self.empty_hint)

    # ------------------------------------------------------------------

    def _pick_current(self) -> None:
        item = self.list.currentItem()
        if item is None and self.list.topLevelItemCount():
            item = self.list.topLevelItem(0)
        if item is not None:
            self._on_item(item)

    def _on_item(self, item: QTreeWidgetItem) -> None:
        part_id = item.data(0, ROLE_PART_ID)
        if part_id is not None:
            self.picked.emit(int(part_id))


# ===========================================================================
#  入库
# ===========================================================================

class StockInDialog(EnterSafeDialog):
    """入库面板：选器件（搜不到就点「＋ 新建器件」）-> 填入库资料。

    继承 EnterSafeDialog：回车不会误触发「入库」默认按钮（见 dialog_base.py）。
    """

    def __init__(self, services: Services, parent: QWidget | None = None,
                 preset_part_id: int | None = None,
                 preset_keyword: str = "") -> None:
        super().__init__(parent)
        self.svc = services
        self.part_id: int | None = None

        self.setWindowTitle("入库")
        self.setMinimumWidth(600)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---- ① 选器件 ----
        box = QGroupBox("① 选器件（没有就点「＋ 新建器件」）")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(8, 8, 8, 8)
        box_layout.setSpacing(6)
        self.search = PartSearch(
            services, empty_hint="没找到。要建新的，点下面的「＋ 新建器件」")
        self.search.picked.connect(self._on_picked)
        box_layout.addWidget(self.search)

        new_row = QHBoxLayout()
        new_row.setContentsMargins(0, 0, 0, 0)
        self.btn_new_part = QPushButton("＋ 新建器件")
        self.btn_new_part.setToolTip(
            "打开完整器件表单（分类 / 型号 / 封装 / 参数 / 关键词都能填），\n"
            "和设置面板「分类管理」里是同一套逻辑。\n"
            "建好后自动选中它，接着往下填入库资料。"
        )
        # 包一层 lambda：clicked 带 bool，直接连会把 False 塞进形参
        # （见 StockOutDialog.add_part 的坑：静默失效、按钮看着像没反应）。
        self.btn_new_part.clicked.connect(lambda: self._new_part_dialog())
        new_row.addWidget(self.btn_new_part)
        new_row.addStretch(1)
        box_layout.addLayout(new_row)
        root.addWidget(box)

        # ---- ② 入库资料 ----
        gb_lot = QGroupBox("② 入库资料")
        form = QFormLayout(gb_lot)
        form.setLabelAlignment(Qt.AlignRight)

        self.lbl_target = QLabel("还没有选中器件")
        self.lbl_target.setWordWrap(True)
        form.addRow("器件", self.lbl_target)

        self.sp_qty = QSpinBox()
        self.sp_qty.setRange(1, 10_000_000)
        self.sp_qty.setValue(1)
        form.addRow("数量 *", self.sp_qty)

        self.cb_location = build_location_combo(services.tree)
        form.addRow("存放位置", combo_row(self.cb_location, self._manage_location))

        # 单价：报价单位可选。
        # 电容电阻这类单价太小（0.0086/颗），照着"元/颗"填得数一串零，容易数错。
        # 立创那种报价单给的是"元/千颗"，所以这里允许按 颗/百颗/千颗 填，
        # **换算后仍按单颗价格存进 stock_lot.unit_price** —— 存储语义一个字没动，
        # 均价、批次价、资产合计、BOM 全都照旧。
        price_row = QWidget()
        price_box = QHBoxLayout(price_row)
        price_box.setContentsMargins(0, 0, 0, 0)
        price_box.setSpacing(6)
        self.ed_price = QLineEdit()
        self.ed_price.setPlaceholderText("填报价，例如 8.6（可留空）")
        price_box.addWidget(self.ed_price, 1)
        self.cb_price_unit = QComboBox()
        for label, factor in PRICE_UNITS:
            self.cb_price_unit.addItem(label, factor)
        self.cb_price_unit.setToolTip(
            "按报价单上的口径填。选「元/千颗」时填 8.6，\n"
            "存进去的就是 0.0086 元/颗（其它地方都按单颗价显示）。"
        )
        price_box.addWidget(self.cb_price_unit)
        form.addRow("单价", price_row)

        self.lbl_price_hint = QLabel("")
        self.lbl_price_hint.setProperty("hint", True)
        self.lbl_price_hint.setWordWrap(True)
        form.addRow("", self.lbl_price_hint)

        # `*_args` 接住 textChanged(str) / currentIndexChanged(int) / valueChanged(int)
        # 带的那个参数 —— 这类带参信号连到有可选形参的槽上会静默传错值（踩过一次）。
        self.ed_price.textChanged.connect(self._update_price_hint)
        self.cb_price_unit.currentIndexChanged.connect(self._update_price_hint)
        self.sp_qty.valueChanged.connect(self._update_price_hint)

        date_row = QWidget()
        date_box = QHBoxLayout(date_row)
        date_box.setContentsMargins(0, 0, 0, 0)
        date_box.setSpacing(6)
        self.ed_date = QLineEdit(_today())
        btn_today = QPushButton("今天")
        btn_today.setFixedWidth(52)
        btn_today.clicked.connect(lambda: self.ed_date.setText(_today()))
        date_box.addWidget(self.ed_date, 1)
        date_box.addWidget(btn_today)
        form.addRow("购买日期", date_row)

        # 厂商和供应商 —— 都是"这一次采购"的属性，不是器件的属性
        self.ed_manufacturer = QLineEdit()
        self.ed_manufacturer.setPlaceholderText("厂牌，例如 YAGEO / 厚声（可留空）")
        form.addRow("厂商", self.ed_manufacturer)

        self.ed_supplier = QLineEdit()
        self.ed_supplier.setPlaceholderText("购买渠道，例如 立创商城 / 淘宝（可留空）")
        form.addRow("供应商", self.ed_supplier)

        self.ed_note = QLineEdit()
        form.addRow("备注", self.ed_note)

        root.addWidget(gb_lot)

        self.chk_merge = QCheckBox("完全相同的采购合并到已有批次")
        self.chk_merge.setChecked(True)
        self.chk_merge.setToolTip(
            "位置、单价、购买日期、厂商、供应商都相同的会合并成一个批次；\n"
            "任何一项不同都新建 —— 那本来就是不同的采购。"
        )
        root.addWidget(self.chk_merge)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("入库")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._apply_remembered()
        self._update_price_hint()       # 计价口径的说明文字要一开始就可见
        if preset_part_id is not None:
            self._on_picked(preset_part_id)
        elif preset_keyword:
            self.search.ed_search.setText(preset_keyword)
            self.search.focus()
        else:
            self.search.focus()

        self._sync_ok()

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------

    def _manage_location(self) -> None:
        dlg = TreeManageDialog(self.svc, "location", self)
        dlg.exec()
        if dlg.changed:
            self._reload_location_combo()

    def _reload_location_combo(self) -> None:
        current = self.cb_location.currentData()
        fill_location_combo(self.cb_location, self.svc.tree)
        if current is not None:
            idx = self.cb_location.findData(current)
            if idx >= 0:
                self.cb_location.setCurrentIndex(idx)

    def _apply_remembered(self) -> None:
        """把上次填的厂商/供应商/位置带出来。

        拆快递是连着录十几种料，每次重敲一遍来源很烦。
        """
        self.ed_manufacturer.setText(config.get("last_manufacturer") or "")
        self.ed_supplier.setText(config.get("last_supplier") or "")
        last_loc = config.get("last_location_id")
        if last_loc is not None:
            index = self.cb_location.findData(last_loc)
            if index >= 0:
                self.cb_location.setCurrentIndex(index)

        # 报价单位也记住：连着录十几种电容时不用每次都重新拨到「元/千颗」
        last_unit = config.get("last_price_unit")
        if last_unit is not None:
            index = self.cb_price_unit.findData(last_unit)
            if index >= 0:
                self.cb_price_unit.setCurrentIndex(index)

    def _update_price_hint(self, *_args) -> None:
        """实时把「报价 × 单位」换算成单颗价，并顺手算这批花多少钱。

        专治"报价单位是千颗、库里存的是单颗"这种口径落差 ——
        填完当场看到 `= ¥0.0086 / 颗`，不用自己心算，也不会存错量级。
        """
        text = self.ed_price.text().strip()
        if not text:
            self.lbl_price_hint.setText(
                "电容电阻这类可以直接照报价单填，选「元/千颗」输 8.6 "
                "= 0.0086 元/颗。留空按 0 记。"
            )
            return
        try:
            value = float(text)
        except ValueError:
            self.lbl_price_hint.setText(f"「{text}」不是有效数字")
            return
        if value < 0:
            self.lbl_price_hint.setText("单价不能是负数")
            return

        unit_price = value / self.cb_price_unit.currentData()
        qty = self.sp_qty.value()
        self.lbl_price_hint.setText(
            f"= ¥{format_money(unit_price)} / 颗"
            f"　·　这批 {qty} 颗合计 ¥{format_money(unit_price * qty)}"
        )

    def _on_picked(self, part_id: int) -> None:
        self.part_id = part_id

        part = self.svc.parts.get(part_id)
        total = self.svc.stock.total_quantity(part_id)
        if part is not None:
            bits = [f"<b>{part.name}</b>"]
            if part.footprint:
                bits.append(f"封装 {part.footprint}")
            if part.mpn:
                bits.append(part.mpn)
            bits.append(f"现有库存 <b>{total}</b>")
            self.lbl_target.setText("　·　".join(bits))
        self._sync_ok()

    def _new_part_dialog(self) -> None:
        """「＋ 新建器件」：打开完整器件表单，建完自动选中它。

        表单（PartEditorDialog）和主窗口、设置面板「分类管理」里共用同一套 ——
        分类 / 参数 / 关键词都能在里面填，从源头消灭"无属性器件"。
        搜索框里已经敲的字会带进表单当默认名称。
        """
        dlg = PartEditorDialog(
            self.svc.parts,
            build_category_labels(self.svc.tree),
            default_name=self.search.keyword(),
            services=self.svc,
            parent=self,
        )
        if not dlg.exec() or dlg.saved_part_id is None:
            return
        part = self.svc.parts.get(dlg.saved_part_id)
        if part is not None:
            # 搜索框切到新器件上（列表同步刷新），然后选中它继续填入库资料
            self.search.ed_search.setText(part.name)
        self.search.refresh()
        self._on_picked(dlg.saved_part_id)

    def _sync_ok(self) -> None:
        button = self.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        button.setEnabled(self.part_id is not None)

    # ------------------------------------------------------------------

    def _on_ok(self) -> None:
        part_id = self.part_id
        if part_id is None:
            return

        price_text = self.ed_price.text().strip()
        try:
            price_quoted = float(price_text) if price_text else 0.0
        except ValueError:
            QMessageBox.warning(self, "单价格式不对", f"「{price_text}」不是有效数字。")
            return
        if price_quoted < 0:
            QMessageBox.warning(self, "单价不能是负数",
                                f"「{price_text}」填成负数了，改一下再存。")
            return
        # 按报价单位换算成单颗价格再入库 —— 库里只有"单颗价格"这一个语义
        price = price_quoted / self.cb_price_unit.currentData()

        # 价格异常提醒：小器件单价太小，多敲一个零就是 10 倍，而且不报错不崩溃，
        # 等对账才发现。**只提醒不拦** —— 真买贵了也得让人入，存储逻辑一个字不改。
        # 均价 > 0 才比得（新器件没有均价可比）；价格留空按 0 记，也不比。
        if price > 0:
            avg = self.svc.stock.average_price(part_id)
            if avg > 0 and (price >= avg * 3 or price * 3 <= avg):
                part = self.svc.parts.get(part_id)
                part_name = part.name if part else str(part_id)
                answer = QMessageBox.question(
                    self, "价格差得有点多",
                    f"你填的单价 ¥{format_money(price)} / 颗，和\n"
                    f"「{part_name}」库里的均价 ¥{format_money(avg)} / 颗\n"
                    f"差了约 {price / avg:.0f} 倍。\n\n"
                    f"小器件单价小，多敲一个零就是 10 倍 —— 确认没填错吗？\n"
                    f"（选「继续」就照常入库，会另起一批，不影响已有批次）",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return

        manufacturer = self.ed_manufacturer.text().strip()
        supplier = self.ed_supplier.text().strip()
        location_id = self.cb_location.currentData()

        try:
            self.svc.stock.add_lot(
                part_id=part_id,
                quantity=self.sp_qty.value(),
                location_id=location_id,
                unit_price=price,
                purchase_date=self.ed_date.text().strip(),
                supplier=supplier,
                manufacturer=manufacturer,
                note=self.ed_note.text().strip(),
                merge_same=self.chk_merge.isChecked(),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("入库失败")
            QMessageBox.critical(self, "入库失败", str(exc))
            return

        # 记住这次填的来源，下次直接带出来
        config.set_value("last_manufacturer", manufacturer)
        config.set_value("last_supplier", supplier)
        config.set_value("last_location_id", location_id)
        config.set_value("last_price_unit", self.cb_price_unit.currentData())

        # 记下器件 id，调用方（主界面）靠它刷新表格并选中该器件
        self.part_id = part_id
        self.accept()


# ===========================================================================
#  出库
# ===========================================================================

class PartPickerDialog(EnterSafeDialog):
    """「加器件」用的选择器：搜到就选（出库的前提是有货，所以没有新建入口）。"""

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.part_id: int | None = None

        self.setWindowTitle("选择器件")
        self.setMinimumWidth(520)
        self.setMinimumHeight(420)

        root = QVBoxLayout(self)
        self.search = PartSearch(services)
        self.search.picked.connect(self._on_picked)
        root.addWidget(self.search)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("加入")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._buttons = buttons
        self.search.focus()

    def _on_picked(self, part_id: int) -> None:
        self.part_id = part_id

    def _on_ok(self) -> None:
        if self.part_id is None:
            QMessageBox.information(self, "还没选", "请先在上面选一个器件。")
            return
        self.accept()


COL_OUT_PART = 0
COL_OUT_QTY = 1
COL_OUT_LOT = 2
COL_OUT_AVAIL = 3
COL_OUT_PRICE = 4
COL_OUT_SUBTOTAL = 5

ROLE_OUT_SUBTOTAL = Qt.UserRole + 4     # 该行小计（float）
ROLE_OUT_MISSING = Qt.UserRole + 5      # 该行有没有缺价（bool）
ROLE_OUT_FIXABLE = Qt.UserRole + 6      # 该行能不能在这儿补价（bool）


class StockOutDialog(EnterSafeDialog):
    """出库面板：可一次出多个器件，每行能指定从哪个批次扣，右下角给合计。

    合计按**实际扣到的批次单价**算（先模拟一次先进先出），不是均价的估算 ——
    跨多个不同价批次时这两个口径差得不小，用户拍板要前者。

    碰到没记价格的批次，那一行整行标黄、合计变黄，**双击单价就地补**
    （写该器件所有在库批次，和 BOM 成本明细同一套逻辑）。
    """

    def __init__(self, services: Services, parent: QWidget | None = None,
                 preset_part_id: int | None = None) -> None:
        super().__init__(parent)
        self.svc = services
        self._updating = False          # 改价后重刷单元格时别被 itemChanged 打回来
        self.changed_price = False      # 有没有真的补过价

        self.setWindowTitle("出库")
        self.setMinimumWidth(760)
        self.setMinimumHeight(460)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(QLabel("用途 / 项目"))
        self.ed_ref = QLineEdit()
        self.ed_ref.setPlaceholderText("例如：平衡车 v1 —— 会记进出库流水，方便以后追溯")
        top.addWidget(self.ed_ref, 1)
        root.addLayout(top)

        add_row = QHBoxLayout()
        self.btn_add = QPushButton("＋ 添加器件")
        self.btn_add.setProperty("accent", True)
        # 必须包一层 lambda：clicked 带一个 bool 参数，而 add_part 的第一个形参是
        # part_id。直接 connect(self.add_part) 的话 PySide6 会把 False 塞进 part_id，
        # `if part_id is None` 判不过 -> 既不弹选择器也不报错，按钮看着像没反应。
        self.btn_add.clicked.connect(lambda: self.add_part())
        add_row.addWidget(self.btn_add)
        self.btn_remove = QPushButton("移除选中行")
        self.btn_remove.clicked.connect(self.remove_selected)
        add_row.addWidget(self.btn_remove)
        add_row.addStretch(1)
        root.addLayout(add_row)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["器件", "出库数量", "扣减批次", "现有库存", "单价 (¥)", "小计 (¥)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 只有「单价」列的格子带 ItemIsEditable（缺价时），其余列双击也编辑不了
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
        )
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.table.setColumnWidth(COL_OUT_PART, 250)
        self.table.setColumnWidth(COL_OUT_QTY, 92)
        self.table.setColumnWidth(COL_OUT_LOT, 240)
        self.table.setColumnWidth(COL_OUT_AVAIL, 92)
        self.table.setColumnWidth(COL_OUT_PRICE, 104)
        root.addWidget(self.table, 1)

        self.lbl_summary = QLabel("还没有添加器件。")
        self.lbl_summary.setProperty("hint", True)
        root.addWidget(self.lbl_summary)

        # 右下角合计：绿 = 每行都算得出来；黄 = 有行的批次没记价格
        total_row = QHBoxLayout()
        total_row.setContentsMargins(0, 0, 0, 0)
        self.lbl_total = QLabel("")
        total_row.addStretch(1)
        total_row.addWidget(self.lbl_total)
        root.addLayout(total_row)

        note = QLabel("不指定批次时按购买日期先后扣（先进先出）。"
                      "任何一行库存不足，整批取消，一颗都不扣。\n"
                      "合计按实际扣到的批次单价算。黄底的行是没记价格的，"
                      "双击「单价」就能补。")
        note.setProperty("hint", True)
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("出库")
        buttons.button(QDialogButtonBox.Ok).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._buttons = buttons

        if preset_part_id is not None:
            self.add_part(preset_part_id)
        self._update_summary()

    # ------------------------------------------------------------------

    def add_part(self, part_id: int | None = None) -> None:
        # 第二道保险：clicked(bool) 这类信号的布尔值（bool 是 int 的子类）不能被
        # 当成器件 id，否则会静默走到 parts.get(False) -> None -> 直接 return。
        if isinstance(part_id, bool) or not isinstance(part_id, int):
            part_id = None
        if part_id is None:
            picker = PartPickerDialog(self.svc, self)
            if not picker.exec():
                return
            part_id = picker.part_id
        if part_id is None:
            return

        existing = self._find_row(part_id)
        if existing is not None:
            self.table.selectRow(existing)
            spin = self.table.cellWidget(existing, COL_OUT_QTY)
            spin.setValue(spin.value() + 1)
            return

        part = self.svc.parts.get(part_id)
        if part is None:
            return

        row = self.table.rowCount()
        self.table.insertRow(row)

        item = QTableWidgetItem(part.name)
        item.setData(ROLE_PART_ID, part_id)
        # 所有格子默认都不可编辑，单价那一格要看情况再把权限加回来
        item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        self.table.setItem(row, COL_OUT_PART, item)

        spin = QSpinBox()
        spin.setRange(1, 10_000_000)
        spin.setValue(1)
        # 数量变了，这一行的「现有库存 / 单价 / 小计」都得跟着算一遍
        # （_refresh_row 末尾会顺带刷合计，所以不用再连 _update_summary）
        spin.valueChanged.connect(lambda _v, r=row: self._refresh_row(r))
        self.table.setCellWidget(row, COL_OUT_QTY, spin)

        combo = QComboBox()
        combo.currentIndexChanged.connect(lambda _i, r=row: self._refresh_row(r))
        self.table.setCellWidget(row, COL_OUT_LOT, combo)

        self.table.setItem(row, COL_OUT_AVAIL, self._read_only_cell(""))
        self.table.setItem(row, COL_OUT_PRICE, self._read_only_cell(""))
        self.table.setItem(row, COL_OUT_SUBTOTAL, self._read_only_cell(""))

        self._fill_lots(row, part_id)
        self.table.selectRow(row)
        self._update_summary()

    @staticmethod
    def _read_only_cell(text: str) -> QTableWidgetItem:
        """一个不可编辑的单元格。QTableWidgetItem 默认带 ItemIsEditable，
        不显式摘掉的话**每一格**都能双击进编辑（BOM 成本明细踩过一次）。"""
        cell = QTableWidgetItem(text)
        cell.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        return cell

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
        self._update_summary()

    def _find_row(self, part_id: int) -> int | None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_OUT_PART)
            if item is not None and item.data(ROLE_PART_ID) == part_id:
                return row
        return None

    def _row_part_id(self, row: int) -> int | None:
        item = self.table.item(row, COL_OUT_PART)
        return item.data(ROLE_PART_ID) if item is not None else None

    def _fill_lots(self, row: int, part_id: int) -> None:
        """批次下拉：自动（先进先出）+ 每个还有货的批次。"""
        combo = self.table.cellWidget(row, COL_OUT_LOT)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("自动（先进先出）", None)
        for lot in self.svc.stock.lots(part_id):
            if lot.quantity <= 0:
                continue
            where = lot.location_label or "未指定位置"
            price = f"  ¥{format_money(lot.unit_price)}" if lot.unit_price else ""
            date = f"  {lot.purchase_date}" if lot.purchase_date else ""
            source = " / ".join(x for x in (lot.manufacturer, lot.supplier) if x)
            combo.addItem(
                f"#{lot.id}　{where}　余 {lot.quantity}{date}{price}"
                + (f"　{source}" if source else ""),
                lot.id,
            )
        combo.setCurrentIndex(0)
        combo.blockSignals(False)
        self._refresh_row(row)

    def _refresh_row(self, row: int) -> None:
        """刷新一行的库存 / 单价 / 小计 / 底色。

        钱的算法：**先模拟一次先进先出的扣减**，按每个批次实际被扣的单价加总。
        所以这里显示的是"真扣的话要花多少钱"，不是均价的估算 —— 两个口径在
        跨批次时会差出一截，用户拍板要前者。
        """
        part_id = self._row_part_id(row)
        if part_id is None:
            return
        # 三格都建好了才算得起来 —— add_part 是逐格 setItem 的，中途会触发刷新
        if any(self.table.item(row, c) is None
               for c in (COL_OUT_AVAIL, COL_OUT_PRICE, COL_OUT_SUBTOTAL)):
            return
        # 接下来要 setText 好几个格子，每个都会触发 itemChanged —— 不挡住的话
        # 会递归进 _on_item_changed 把刚算好的数又刷一遍
        self._updating = True
        try:
            total = self.svc.stock.total_quantity(part_id)
            spin = self.table.cellWidget(row, COL_OUT_QTY)
            want = spin.value()

            combo = self.table.cellWidget(row, COL_OUT_LOT)
            lot_id = combo.currentData()
            if lot_id is None:
                have = total
            else:
                lot = self.svc.stock.lot(lot_id)
                have = lot.quantity if lot else 0

            allocation = self.svc.stock.preview_withdraw_cost(part_id, want, lot_id)
            allocated_qty = sum(qty for _, qty, _ in allocation)
            subtotal = sum(qty * price for _, qty, price in allocation)
            missing_price = any(price <= 0 for _, _, price in allocation)
            # 单价要除以**真正扣得到的数量**，不是你填的数量。
            # 库存不够时扣减会被截断到现有量 —— 这时候再除以 want，
            # 数量填得越大单价就被稀释得越低，而小计却停着不动，
            # 看起来就像"单价在降、总价不动"（用户实际撞到的就是这个）。
            unit_avg = (subtotal / allocated_qty) if allocated_qty else 0.0

            palette = theme.pal()
            enough = have >= want
            item = self.table.item(row, COL_OUT_AVAIL)
            item.setText(f"{have} / 需 {want}")
            item.setForeground(QColor(palette.fg_normal if enough else palette.fg_out))
            item.setToolTip("" if enough else "这个来源不够，出库会被整批取消")

            # 单价列显示的是"这一行平均下来一颗多少钱"（跨批次时是加权平均），
            # 逐批次的拆解放 tooltip，不占列
            price_item = self.table.item(row, COL_OUT_PRICE)
            price_item.setText(format_money(unit_avg) if subtotal > 0 else "—")
            price_item.setToolTip(
                "按先进先出扣到的批次：\n"
                + ("\n".join(
                    f"　· 批次#{lot_id_}　扣 {qty}　@ ¥{format_money(price)}"
                    for lot_id_, qty, price in allocation
                ) or "还没有可扣的批次")
            )

            sub_item = self.table.item(row, COL_OUT_SUBTOTAL)
            sub_item.setText(format_money(subtotal) if allocation else "—")
            if not enough:
                sub_item.setToolTip(
                    f"这个来源只有 {have} 颗，你要 {want} 颗 ——\n"
                    f"上面的小计是**现有这些**的钱，不是你要的量的钱。\n"
                    f"出库会被整批取消，先改数量或换批次。")
            else:
                sub_item.setToolTip("")

            # 数值挂角色上，合计直接读 —— 别去解析单元格文本，太脆
            sub_item.setData(ROLE_OUT_SUBTOTAL, float(subtotal))
            can_fix = missing_price and total > 0
            price_item.setData(ROLE_OUT_MISSING, bool(missing_price))
            price_item.setData(ROLE_OUT_FIXABLE, bool(can_fix))

            self._tint_row(row, missing_price)
            if can_fix:
                price_item.setFlags(price_item.flags() | Qt.ItemIsEditable)
                price_item.setToolTip(
                    price_item.toolTip()
                    + "\n\n这一行里有批次没记价格 —— 双击「单价」补上，"
                      "会写到这个器件所有在库批次。"
                )
            else:
                price_item.setFlags(price_item.flags() & ~Qt.ItemIsEditable)
        finally:
            self._updating = False
        self._update_summary()

    def _tint_row(self, row: int, missing_price: bool) -> None:
        """缺价的行整行标黄，一眼能看出这行的钱是算不出来的。"""
        tint = QColor(theme.pal().bg_low) if missing_price else None
        for col in range(self.table.columnCount()):
            item = self.table.item(row, col)
            if item is not None:
                item.setBackground(tint if tint is not None else QColor(Qt.transparent))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """在出库表格里就地补价 —— 和 BOM 成本明细同一套落库逻辑。"""
        if self._updating or item.column() != COL_OUT_PRICE:
            return
        row = item.row()
        if row >= self.table.rowCount():
            return
        part_id = self._row_part_id(row)
        if part_id is None:
            return
        # 没补价资格的格子根本不该进编辑态。注意 **setItem 本身就会触发
        # itemChanged**（从无到有也算"变了"），add_part 建格子时每个都会走到这。
        # 这里重刷一次是安全的：_refresh_row 开头有三格齐全的守卫，
        # 建到一半时它自己会退出，不会像以前那样摸到 None 崩掉。
        if not item.data(ROLE_OUT_FIXABLE):
            self._refresh_row(row)
            return

        text = item.text().strip()
        if text in ("", "—"):
            self._refresh_row(row)      # 清空 / 没动，不算改价
            return
        try:
            value = float(text)
        except ValueError:
            QMessageBox.warning(self, "单价格式不对", f"「{text}」不是有效数字。")
            self._refresh_row(row)
            return
        if value < 0:
            QMessageBox.warning(self, "单价不能是负数", f"「{text}」填成负数了。")
            self._refresh_row(row)
            return

        try:
            touched = self.svc.stock.set_unit_price(part_id, value)
        except Exception as exc:  # noqa: BLE001
            log.exception("出库面板改价失败")
            QMessageBox.critical(self, "改价失败", str(exc))
            self._refresh_row(row)
            return
        if touched <= 0:
            QMessageBox.information(
                self, "没改成", "这个器件没有数量大于 0 的批次，单价没有落点。先去入库。")
            self._refresh_row(row)
            return

        self.changed_price = True
        # 这个器件的每一行单价都变了，整列重算
        self._updating = True
        try:
            for r in range(self.table.rowCount()):
                if self._row_part_id(r) == part_id:
                    self._refresh_row(r)
        finally:
            self._updating = False
        self._update_total()

    def _update_total(self) -> None:
        """右下角合计：绿 = 每行都算得出来；黄 = 有行没价。"""
        pal = theme.pal()
        rows = self.table.rowCount()
        if rows == 0:
            self.lbl_total.setText("")
            self.lbl_total.setStyleSheet("")
            return
        total = sum(self.table.item(r, COL_OUT_SUBTOTAL).data(ROLE_OUT_SUBTOTAL) or 0.0
                    for r in range(rows))
        missing = sum(1 for r in range(rows)
                      if self.table.item(r, COL_OUT_PRICE).data(ROLE_OUT_MISSING))

        bits = [f"合计 ¥{format_money_total(total)}"]
        if missing:
            bits.append(f"{missing} 行没价")
        self.lbl_total.setText("　·　".join(bits))
        self.lbl_total.setStyleSheet(
            f"color: {pal.fg_ok if not missing else pal.fg_attention}; font-weight: 500;"
        )

    def _update_summary(self) -> None:
        rows = self.table.rowCount()
        if rows == 0:
            self.lbl_summary.setText("还没有添加器件。")
            self._update_total()
            return
        total_qty = sum(self.table.cellWidget(r, COL_OUT_QTY).value() for r in range(rows))
        shortage = 0
        for row in range(rows):
            part_id = self._row_part_id(row)
            if part_id is None:
                continue
            combo = self.table.cellWidget(row, COL_OUT_LOT)
            lot_id = combo.currentData()
            have = (self.svc.stock.total_quantity(part_id) if lot_id is None
                    else (self.svc.stock.lot(lot_id).quantity
                          if self.svc.stock.lot(lot_id) else 0))
            if have < self.table.cellWidget(row, COL_OUT_QTY).value():
                shortage += 1
        text = f"{rows} 个器件 / 合计 {total_qty} 颗"
        if shortage:
            text += f"　·　{shortage} 行库存不足"
        self.lbl_summary.setText(text)
        self._update_total()

    # ------------------------------------------------------------------

    def _on_ok(self) -> None:
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "还没添加器件", "请先「＋ 添加器件」。")
            return

        ref = self.ed_ref.text().strip()
        requests: list[tuple[int, int, str, int | None]] = []
        for row in range(self.table.rowCount()):
            part_id = self._row_part_id(row)
            if part_id is None:
                continue
            qty = self.table.cellWidget(row, COL_OUT_QTY).value()
            lot_id = self.table.cellWidget(row, COL_OUT_LOT).currentData()
            requests.append((part_id, qty, ref, lot_id))

        total_qty = sum(q for _, q, _, _ in requests)
        # 确认框里把钱也带上 —— 这是"这次出库实际花了多少"的最后一眼
        total_money = sum(
            self.table.item(r, COL_OUT_SUBTOTAL).data(ROLE_OUT_SUBTOTAL) or 0.0
            for r in range(self.table.rowCount())
        )
        if QMessageBox.question(
            self, "确认出库",
            f"将出库 {len(requests)} 个器件、合计 {total_qty} 颗"
            f"、金额 ¥{format_money_total(total_money)}。\n\n"
            f"采用先进先出，整批在一个事务里。\n"
            f"任何一行库存不足则整批取消，一颗都不扣。\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        try:
            self.svc.stock.withdraw_many(requests)
        except BulkInsufficientStockError as exc:
            names = []
            for pid, need, have in exc.shortages:
                part = self.svc.parts.get(pid)
                names.append(f"  · {part.name if part else pid}：需要 {need}，现有 {have}")
            QMessageBox.warning(
                self, "库存不足，整批未出库",
                f"以下 {len(exc.shortages)} 项不够，一颗都没扣：\n\n"
                + "\n".join(names) + "\n\n补齐后重新执行即可。",
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("出库失败")
            QMessageBox.critical(self, "出库失败", str(exc))
            return

        self.accept()


# ===========================================================================
#  分类 / 存放位置 管理
# ===========================================================================

ROLE_NODE_ID = Qt.UserRole + 3


def _friendly_tree_error(exc: Exception) -> str:
    """把数据库的约束报错翻译成能懂的话。

    用户撞到的是 `UNIQUE constraint failed: index 'ux_location_name'` ——
    这话没人看得懂，其实就是在说"这个名字已经存在了"。
    """
    text = str(exc)
    if "ux_category_name" in text or "ux_location_name" in text:
        return "这个名字在同级里已经有了，换一个吧。"
    if "ux_location_code" in text:
        return "这个短码已经被别的位置用了。"
    return text


class TreeManageDialog(QDialog):
    """分类 / 存放位置的管理：增、重命名、删。

    两棵树共用一个对话框，`kind` 决定走哪一套（category / location）。
    位置就是**纯文本名字**，不要编码（用户明确要求）——
    库表里的 `code` 列留着不删，但界面上不再出现。

    **删除是安全的，界面上要说清楚**：
        删分类  子分类一起删；器件**不删**，category_id 被置空成「未分类」
        删位置  子位置一起删；批次**不删**，location_id 被置空成「未指定」

    名称用**同一个输入框**：新增时是"要加的"，选中后点重命名就是"改成"。
    这样不用再弹一层 QInputDialog，一屏之内能完成所有操作。
    """

    def __init__(self, services: Services, kind: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if kind not in ("category", "location"):
            raise ValueError(f"未知的树类型：{kind}")
        self.svc = services
        self.tree_svc = services.tree
        self.kind = kind
        self.changed = False            # 有没有真的动过 —— 调用方据此决定要不要重填下拉

        self.setWindowTitle("存放位置管理" if kind == "location" else "分类管理")
        self.setMinimumSize(540, 480)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["名称"])
        self.tree.setAlternatingRowColors(False)
        self.tree.setColumnWidth(0, 400)
        self.tree.itemSelectionChanged.connect(self._on_select)
        root.addWidget(self.tree, 1)

        self.lbl_hint = QLabel("")
        self.lbl_hint.setProperty("hint", True)
        self.lbl_hint.setWordWrap(True)
        root.addWidget(self.lbl_hint)

        form_row = QHBoxLayout()
        form_row.setSpacing(6)
        form_row.addWidget(QLabel("名称"))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText(
            "例如：贴片电阻" if kind == "category" else "例如：元件柜B / B3-模块")
        form_row.addWidget(self.ed_name, 1)
        root.addLayout(form_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        btn_top = QPushButton("新增顶层")
        btn_top.clicked.connect(lambda: self._add(None))
        buttons.addWidget(btn_top)
        btn_sub = QPushButton("新增子项")
        btn_sub.setToolTip("在树上选中的那个下面加一层（比如 元件柜B 下面加 B3-模块）")
        btn_sub.clicked.connect(lambda: self._add(self._selected_id()))
        buttons.addWidget(btn_sub)
        btn_rename = QPushButton("重命名选中")
        btn_rename.clicked.connect(self._rename)
        buttons.addWidget(btn_rename)
        btn_del = QPushButton("删除选中")
        btn_del.setProperty("danger", True)
        btn_del.clicked.connect(self._delete)
        buttons.addWidget(btn_del)
        buttons.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        buttons.addWidget(btn_close)
        root.addLayout(buttons)

        self._reload()

    # ------------------------------------------------------------------

    def _selected_id(self) -> int | None:
        item = self.tree.currentItem()
        return item.data(0, ROLE_NODE_ID) if item is not None else None

    def _on_select(self) -> None:
        """选中就把名字带进输入框，改起来顺手。"""
        node = self.tree.currentItem()
        if node is None:
            self.lbl_hint.setText("")
            return
        self.ed_name.setText(node.text(0))
        path = (self.tree_svc.location_path(self._selected_id())
                if self.kind == "location"
                else self.tree_svc.category_path(self._selected_id()))
        self.lbl_hint.setText(f"选中：{path}")

    def _reload(self) -> None:
        self.tree.clear()
        entries = (self.tree_svc.list_categories() if self.kind == "category"
                   else self.tree_svc.list_locations())
        by_parent: dict = {}
        for entry in entries:
            by_parent.setdefault(entry.parent_id, []).append(entry)

        def add_level(parent_id, parent_item) -> None:
            for entry in sorted(by_parent.get(parent_id, []),
                                key=lambda e: e.name.lower()):
                texts = [entry.name]
                node = QTreeWidgetItem(texts)
                node.setData(0, ROLE_NODE_ID, entry.id)
                if parent_item is None:
                    self.tree.addTopLevelItem(node)
                else:
                    parent_item.addChild(node)
                add_level(entry.id, node)

        add_level(None, None)
        self.tree.expandAll()

    def _add(self, parent_id: int | None) -> None:
        name = self.ed_name.text().strip()
        if not name:
            QMessageBox.information(self, "先填名称", "在下面的「名称」格里填好再点新增。")
            self.ed_name.setFocus()
            return
        try:
            if self.kind == "category":
                self.tree_svc.create_category(name, parent_id)
            else:
                self.tree_svc.create_location(name, parent_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("新增分类/位置失败")
            QMessageBox.critical(self, "新增失败", _friendly_tree_error(exc))
            return
        self.ed_name.clear()
        self.changed = True
        self._reload()

    def _rename(self) -> None:
        node_id = self._selected_id()
        if node_id is None:
            QMessageBox.information(self, "先选中", "先在树上选中要改的那个。")
            return
        name = self.ed_name.text().strip()
        if not name:
            QMessageBox.information(self, "先填名称", "把新名字填在「名称」格里再点重命名。")
            return
        try:
            if self.kind == "category":
                self.tree_svc.rename_category(node_id, name)
            else:
                self.tree_svc.rename_location(node_id, name)
        except Exception as exc:  # noqa: BLE001
            log.exception("重命名分类/位置失败")
            QMessageBox.critical(self, "重命名失败", _friendly_tree_error(exc))
            return
        self.changed = True
        self._reload()

    def _delete(self) -> None:
        node_id = self._selected_id()
        if node_id is None:
            QMessageBox.information(self, "先选中", "先在树上选中要删的那个。")
            return

        # 把影响说清楚再让人按确认 —— 这两个删除都是"引用置空"，不连带删数据
        if self.kind == "category":
            ids = self.tree_svc.category_ids_with_children(node_id)
            marks = ",".join("?" * len(ids))
            n_parts = self.svc.db.query_one(
                f"SELECT COUNT(*) AS n FROM part WHERE category_id IN ({marks})", ids
            )["n"]
            name = self.tree_svc.category_path(node_id)
            message = (f"删除「{name}」？\n\n"
                       f"· 它下面的子分类会一起删\n"
                       f"· 挂在它（含子分类）下面的 {n_parts} 个器件**不会被删**，"
                       f"只是变成「未分类」\n")
        else:
            ids = self.tree_svc.location_ids_with_children(node_id)
            marks = ",".join("?" * len(ids))
            n_lots = self.svc.db.query_one(
                f"SELECT COUNT(*) AS n FROM stock_lot WHERE location_id IN ({marks})", ids
            )["n"]
            name = self.tree_svc.location_path(node_id)
            message = (f"删除「{name}」？\n\n"
                       f"· 它下面的子位置会一起删\n"
                       f"· 放在这里的 {n_lots} 个批次**不会被删**，"
                       f"只是变成「未指定位置」\n")

        if QMessageBox.question(
            self, "确认删除", message + "\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        try:
            if self.kind == "category":
                self.tree_svc.delete_category(node_id)
            else:
                self.tree_svc.delete_location(node_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("删除分类/位置失败")
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        self.lbl_hint.setText("")
        self.changed = True
        self._reload()
