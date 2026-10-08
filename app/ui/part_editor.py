"""新建 / 编辑器件的对话框 —— 全项目只有这一套器件表单。

三个入口都用它：主窗口的编辑 / 新建、入库面板的「＋ 新建器件」、
设置面板「分类管理」页签右栏的器件编辑与新建。入库面板原来那个只收 4 个字段的
内嵌小表单就是"无属性器件"的来源，已删除。

参数区是**动态**的：选中"电容"就出现容值/耐压/介质，选中"电阻"就换成阻值/精度。
字段定义来自 param_template，跟具体器件无关——这就是模板继承在界面上的体现。

数值参数用 QLineEdit 而不是 QDoubleSpinBox：spinbox 没法表达"这一项没填"，
会强行塞个 0 进去，然后 0 就被当成一个真实的规格值存下来了。宁可要空。

分类下拉旁边的「…」打开分类管理（一级/二级，增/重命名/删）。
"没有合适的分类"不再需要退出表单另找入口 —— 就地建完原地回填。

继承 EnterSafeDialog：回车不会误触发「保存」（根因见 dialog_base.py）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.domain import Services
from app.domain.models import Part
from app.domain.part_service import PartService
from app.ui.category_manager import CategoryManagerDialog
from app.ui.dialog_base import EnterSafeDialog

log = logging.getLogger("edms.ui.part_editor")


class PartEditorDialog(EnterSafeDialog):
    def __init__(
        self,
        parts: PartService,
        category_labels: list[tuple[int, str]],
        part_id: int | None = None,
        default_category_id: int | None = None,
        default_name: str = "",
        services: Services | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.parts = parts
        self.part_id = part_id
        self._services = services        # 分类「…」要用（拿不到就藏起入口）
        self._param_editors: dict[str, tuple[QLineEdit, int, str]] = {}
        self._saved_params: dict[str, str] = {}

        self.setWindowTitle("编辑器件" if part_id else "新建器件")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ---------------- 基本信息 ----------------
        basic = QGroupBox("基本信息")
        form = QFormLayout(basic)
        form.setLabelAlignment(Qt.AlignRight)

        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("例如：10kΩ 0603 1% 电阻")
        form.addRow("名称 *", self.ed_name)

        self.cb_category = QComboBox()
        self.cb_category.addItem("（未分类）", None)
        for cid, label in category_labels:
            self.cb_category.addItem(label, cid)
        self.cb_category.currentIndexChanged.connect(self._rebuild_params)
        if services is not None:
            # 分类下拉 + 「…」：没有合适的分类**就地建**（一级/二级），
            # 建完原地重填下拉、保住当前选中 —— 不用退出表单另找入口。
            cat_row = QWidget()
            cat_h = QHBoxLayout(cat_row)
            cat_h.setContentsMargins(0, 0, 0, 0)
            cat_h.setSpacing(4)
            cat_h.addWidget(self.cb_category, 1)
            btn_manage = QPushButton("…")
            btn_manage.setFixedWidth(30)
            btn_manage.setToolTip("管理分类：新增 / 重命名 / 删除（一级 / 二级）")
            # 包 lambda：clicked 带 bool（同 stock_dialog 里 add_part 的坑）
            btn_manage.clicked.connect(lambda: self._manage_category())
            cat_h.addWidget(btn_manage)
            form.addRow("分类", cat_row)
        else:
            form.addRow("分类", self.cb_category)

        self.ed_mpn = QLineEdit()
        self.ed_mpn.setPlaceholderText("厂商型号 / 料号，例如 RC0603FR-0710KL")
        form.addRow("型号", self.ed_mpn)

        # 厂商**故意不放这里**：同一个规格可能从不同厂牌采购
        # （10kΩ 有 YAGEO 的也有厚声的），那属于"这一次采购"的属性，
        # 记在入库批次上，否则每换一个厂牌就得新建一个器件。
        mfr_hint = QLabel("在「入库」时记录 —— 同规格换厂牌不算新器件")
        mfr_hint.setProperty("hint", True)
        mfr_hint.setWordWrap(True)
        form.addRow("厂商", mfr_hint)

        # 封装做成可编辑下拉：常见值直接选，新封装直接敲
        self.cb_footprint = QComboBox()
        self.cb_footprint.setEditable(True)
        self.cb_footprint.addItem("")
        for fp in ("0201", "0402", "0603", "0805", "1206", "1210",
                   "SOT-23", "SOT-89", "SOP-8", "SOIC-8", "SSOP-20",
                   "LQFP-32", "LQFP-48", "LQFP-64", "QFN-32",
                   "TO-220", "TO-252", "DIP-8", "DO-214AC(SMA)"):
            self.cb_footprint.addItem(fp)
        form.addRow("封装", self.cb_footprint)

        self.sp_min = QSpinBox()
        self.sp_min.setRange(0, 1_000_000)
        self.sp_min.setSpecialValueText("不预警")
        form.addRow("库存预警阈值", self.sp_min)

        self.ed_keywords = QLineEdit()
        self.ed_keywords.setPlaceholderText("别名，空格分隔。例如：res 电阻 贴片")
        form.addRow("关键词", self.ed_keywords)

        self.ed_desc = QPlainTextEdit()
        self.ed_desc.setPlaceholderText("备注、替代型号、用途……")
        self.ed_desc.setFixedHeight(60)
        form.addRow("备注", self.ed_desc)

        root.addWidget(basic)

        # ---------------- 规格参数（动态） ----------------
        self.gb_params = QGroupBox("规格参数")
        self.param_form = QFormLayout(self.gb_params)
        self.param_form.setLabelAlignment(Qt.AlignRight)
        root.addWidget(self.gb_params)

        self.lbl_no_template = QLabel(
            "该分类还没有定义参数模板。\n"
            "需要的话可以到「分类管理」里为该分类添加参数（如阻值、精度）。"
        )
        self.lbl_no_template.setProperty("hint", True)
        self.lbl_no_template.setWordWrap(True)
        self.lbl_no_template.hide()
        root.addWidget(self.lbl_no_template)

        # ---------------- 按钮 ----------------
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Save).setProperty("accent", True)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # ---------------- 装载数据 ----------------
        if part_id is not None:
            self._load(part_id)
        else:
            if default_name:
                # 带出来的名字（例如入库面板搜索框里敲的）直接选中：
                # 想改就接着敲，想留就直接补别的字段
                self.ed_name.setText(default_name)
                self.ed_name.setFocus()
                self.ed_name.selectAll()
            if default_category_id is not None:
                idx = self.cb_category.findData(default_category_id)
                if idx >= 0:
                    self.cb_category.setCurrentIndex(idx)

        self._rebuild_params()

    # ==================================================================
    #  装载
    # ==================================================================

    def _load(self, part_id: int) -> None:
        part = self.parts.get(part_id)
        if part is None:
            raise ValueError(f"器件不存在：{part_id}")

        self.ed_name.setText(part.name)
        idx = self.cb_category.findData(part.category_id)
        self.cb_category.setCurrentIndex(idx if idx >= 0 else 0)

        self.ed_mpn.setText(part.mpn)
        self.cb_footprint.setCurrentText(part.footprint)
        self.sp_min.setValue(int(part.min_stock or 0))
        self.ed_keywords.setText(part.keywords)
        self.ed_desc.setPlainText(part.description)

        # 先把已有参数按"模板名"记下来，等下重建表单时按名字回填，
        # 这样即使用户中途切换分类再切回来，填过的东西也不会丢。
        for pv in self.parts.get_params(part_id):
            self._saved_params[pv.name] = pv.display

    # ==================================================================
    #  分类管理（分类行「…」）
    # ==================================================================

    def _manage_category(self) -> None:
        if self._services is None:
            return
        dlg = CategoryManagerDialog(self._services, self)
        dlg.exec()
        if dlg.changed:
            self._reload_category_combo()

    def _reload_category_combo(self) -> None:
        """管理完分类后原地重填下拉，尽量保住当前选中的那一个。"""
        if self._services is None:
            return
        current = self.cb_category.currentData()
        self.cb_category.blockSignals(True)
        self.cb_category.clear()
        self.cb_category.addItem("（未分类）", None)
        for cid, label in build_category_labels(self._services.tree):
            self.cb_category.addItem(label, cid)
        idx = self.cb_category.findData(current) if current is not None else 0
        self.cb_category.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_category.blockSignals(False)
        # 选中的分类可能被删了/改名了，参数区必须按新的选择重建一次
        self._rebuild_params()

    # ==================================================================
    #  动态参数区
    # ==================================================================

    def _collect_current_params(self) -> None:
        for name, (editor, _tid, _dtype) in self._param_editors.items():
            self._saved_params[name] = editor.text()

    def _clear_param_form(self) -> None:
        while self.param_form.rowCount():
            self.param_form.removeRow(0)
        self._param_editors.clear()

    def _rebuild_params(self) -> None:
        self._collect_current_params()
        self._clear_param_form()

        category_id = self.cb_category.currentData()
        templates = self.parts.templates_for_category(category_id)

        if not templates:
            self.gb_params.hide()
            self.lbl_no_template.setVisible(category_id is not None)
            return

        self.gb_params.show()
        self.lbl_no_template.hide()

        for tpl in templates:
            editor = QLineEdit()
            editor.setPlaceholderText("数值" if tpl.data_type == "num" else "文本")

            row = QWidget()
            hbox = QHBoxLayout(row)
            hbox.setContentsMargins(0, 0, 0, 0)
            hbox.setSpacing(6)
            hbox.addWidget(editor, 1)

            if tpl.unit:
                unit_label = QLabel(tpl.unit)
                unit_label.setProperty("hint", True)
                unit_label.setMinimumWidth(36)
                hbox.addWidget(unit_label)

            self.param_form.addRow(tpl.name, row)

            # 回填：去掉单位后缀，只留数值部分
            previous = self._saved_params.get(tpl.name, "")
            if previous:
                suffix = f" {tpl.unit}".strip()
                if suffix and previous.endswith(suffix):
                    previous = previous[: -len(suffix)]
                elif previous.endswith(tpl.unit) and tpl.unit:
                    previous = previous[: -len(tpl.unit)]
                editor.setText(previous.strip())

            self._param_editors[tpl.name] = (editor, tpl.id, tpl.data_type)

    def _parse_params(self) -> dict[int, float | str | None]:
        """把界面上的文本转成 service 要的类型。空字符串 -> None（等于删除该参数）。"""
        result: dict[int, float | str | None] = {}
        for name, (editor, template_id, dtype) in self._param_editors.items():
            raw = editor.text().strip()
            if not raw:
                result[template_id] = None
                continue
            if dtype == "num":
                try:
                    result[template_id] = float(raw)
                except ValueError:
                    raise ValueError(f"参数「{name}」需要填数字，当前是 “{raw}”") from None
            else:
                result[template_id] = raw
        return result

    # ==================================================================
    #  保存
    # ==================================================================

    def _on_save(self) -> None:
        try:
            params = self._parse_params()
        except ValueError as exc:
            QMessageBox.warning(self, "参数填写有误", str(exc))
            return

        part = Part(
            id=self.part_id,
            name=self.ed_name.text().strip(),
            category_id=self.cb_category.currentData(),
            mpn=self.ed_mpn.text().strip(),
            footprint=self.cb_footprint.currentText().strip(),
            description=self.ed_desc.toPlainText().strip(),
            keywords=self.ed_keywords.text().strip(),
            min_stock=self.sp_min.value(),
        )

        if not part.name:
            QMessageBox.warning(self, "缺少名称", "器件名称不能为空。")
            self.ed_name.setFocus()
            return

        try:
            if self.part_id is None:
                self.part_id = self.parts.create(part, params)
            else:
                self.parts.update(part, params)
        except Exception as exc:  # noqa: BLE001 - 兜底，把 DB 层异常变成界面提示
            log.exception("保存器件失败")
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        self.accept()

    @property
    def saved_part_id(self) -> int | None:
        return self.part_id


def build_category_labels(tree) -> list[tuple[int, str]]:
    """把分类树拍平成 [(id, "电阻 / 贴片电阻"), ...]，按路径排序。"""
    pairs = [
        (c.id, tree.category_path(c.id) or c.name)
        for c in tree.list_categories()
    ]
    pairs.sort(key=lambda p: p[1])
    return pairs


def confirm_and_delete_part(services: Services, part_id: int,
                            parent: QWidget | None = None) -> bool:
    """确认后删除器件（批次 / 参数 / 流水级联删），返回是否真的删了。

    主窗口的「删除」按钮是现在唯一的调用方 ——
    确认框必须把"连带损失"写清楚：几个批次、多少颗库存、流水一起没。
    """
    part = services.parts.get(part_id)
    if part is None:
        return False

    total = services.stock.total_quantity(part.id)
    detail = ""
    if total:
        n_lots = len(services.stock.lots(part.id))
        detail = (f"\n\n它还有 {total} 颗库存分布在 {n_lots} 个批次里，"
                  f"连同全部出入库流水都会被一起删除。")

    answer = QMessageBox.question(
        parent, "确认删除",
        f"确定删除「{part.name}」吗？{detail}\n\n此操作不可撤销。",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
    )
    if answer != QMessageBox.Yes:
        return False

    services.parts.delete(part.id)
    return True
