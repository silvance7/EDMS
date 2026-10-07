"""BOM 操作的「导出 / 导入」两个页签。

挂在 `bom_dialog.BomDialog` 的 QTabWidget 里，和「比对」页签共用入口。
两个面板都只做"选文件 / 显示结果"，真正的读写逻辑全在
`app.domain.part_io`（那边不依赖 Qt，可以无头测）。

交互刻意保持和现有对话框一致的风格：
- 动作按钮在主行，"危险"或"关键"动作有确认框
- 结果不是一句"成功"，而是把「建了几个 / 入了多少 / 哪几行失败了」摆全
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.domain import Services
from app.domain.part_io import (
    ImportPlan,
    ImportReport,
    collect_export_rows,
    execute_import,
    export_summary,
    plan_import,
    read_import_rows,
    write_export_csv,
)

log = logging.getLogger("edms.ui.bom_io")


def _start_dir() -> str:
    """文件对话框的默认起点：上次用过 BOM 的目录，否则退回用户主目录。"""
    start = config.get("bom_dir") or ""
    return start if start and Path(start).exists() else str(Path.home())


# ===========================================================================
#  导出页
# ===========================================================================

class BomExportPanel(QWidget):
    """把当前库存导成一份 CSV 器件清单。"""

    def __init__(self, svc: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = svc

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        intro = QLabel(
            "把当前库存里的**全部器件**导出成一份 CSV 清单（每个批次一行）。\n"
            "可以在 Excel / WPS 里查看、修数据，改完从「导入」页签原样导回来。"
        )
        intro.setTextFormat(Qt.MarkdownText)
        intro.setWordWrap(True)
        v.addWidget(intro)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setProperty("title", True)
        self.lbl_stats.setWordWrap(True)
        v.addWidget(self.lbl_stats)

        self.lbl_detail = QLabel("")
        self.lbl_detail.setProperty("hint", True)
        self.lbl_detail.setWordWrap(True)
        v.addWidget(self.lbl_detail)

        v.addStretch(1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.btn_export = QPushButton("导出为 CSV…")
        self.btn_export.setProperty("accent", True)
        self.btn_export.setToolTip("导出后可以在 Excel 里直接打开（UTF-8 编码，中文不乱码）")
        self.btn_export.clicked.connect(self.export)
        row.addWidget(self.btn_export)
        v.addLayout(row)

        self.refresh_stats()

    # ------------------------------------------------------------------

    def refresh_stats(self) -> None:
        """重算库存统计。构造时、导出完成后、以及从「导入」页签导进了新器件时
        都要调一次 —— 数据量小（几百行），直接整份收集一遍，比单独写统计 SQL 干净。
        """
        rows = collect_export_rows(self.svc)
        parts, lines = export_summary(rows)
        self._rows = rows

        if not rows:
            self.lbl_stats.setText("库里还没有任何器件，暂时没有可导出的内容。")
            self.lbl_detail.setText("")
            self.btn_export.setEnabled(False)
            return

        no_stock = sum(1 for r in rows if r.qty <= 0)
        self.lbl_stats.setText(f"当前库存：{parts} 个器件 · {lines} 条导出记录")
        detail = "导出列：器件名称 / 型号 / 封装 / 分类 / 数量 / 位置 / 单价 / 购买日期 / 厂商 / 供应商 / 备注"
        if no_stock:
            detail += f"\n其中 {no_stock} 条是「在册但无货」的器件（数量为 0）。"
        self.lbl_detail.setText(detail)
        self.btn_export.setEnabled(True)

    def _ask_save_path(self) -> str:
        """弹保存对话框。单独抽成实例方法是为了测试能替换掉 ——
        QFileDialog.getSaveFileName 是静态方法，patch 类属性在 PySide6 里不生效
        （和 main_window._exec_lot_menu 是同一个坑，见项目踩坑记录 #17）。"""
        default_path = str(Path(_start_dir()) / f"器件清单_{date.today():%Y%m%d}.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出器件清单", default_path, "CSV 文件 (*.csv)"
        )
        return path

    def export(self) -> None:
        rows = collect_export_rows(self.svc)
        if not rows:
            QMessageBox.information(self, "没有可导出的器件", "库里还没有任何器件。")
            return

        path = self._ask_save_path()
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"

        try:
            write_export_csv(path, rows)
        except Exception as exc:  # noqa: BLE001
            log.exception("导出器件清单失败")
            QMessageBox.critical(self, "导出失败", f"写文件时出错：\n\n{exc}")
            return

        config.set_value("bom_dir", str(Path(path).parent))
        parts, lines = export_summary(rows)
        QMessageBox.information(
            self, "导出完成",
            f"已导出 {parts} 个器件、{lines} 条记录。\n\n{path}\n\n"
            f"用 Excel / WPS 双击打开即可；改完从「导入」页签导回来。",
        )
        self.refresh_stats()


# ===========================================================================
#  导入页
# ===========================================================================

class BomImportPanel(QWidget):
    """从一份器件清单批量导入：没有的建档，带数量的按批次规则入库。"""

    # 导入执行完（无论有没有部分失败）后发出 —— BomDialog 用它刷新
    # 「比对」页签的器件下拉框缓存，不然新导进来的器件在下拉框里选不到。
    imported = Signal()

    def __init__(self, svc: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = svc
        self._path: Path | None = None
        self._rows = []
        self._parse_errors: list[str] = []

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        intro = QLabel(
            "把一份器件清单（CSV / xlsx）批量导进来：**库里没有的建档**，"
            "带数量的行按批次规则入库。\n"
            "表头至少要有「器件名称」列；「数量 / 位置 / 单价 / 购买日期 / 厂商 / "
            "供应商 / 备注 / 型号 / 封装 / 分类 / 关键词」这些列认得出来就会一并导入。"
        )
        intro.setTextFormat(Qt.MarkdownText)
        intro.setWordWrap(True)
        v.addWidget(intro)

        file_row = QHBoxLayout()
        file_row.setContentsMargins(0, 0, 0, 0)
        file_row.setSpacing(8)
        self.btn_choose = QPushButton("选择文件…")
        self.btn_choose.clicked.connect(self.choose_file)
        file_row.addWidget(self.btn_choose)
        self.lbl_file = QLabel("（未选择文件）")
        self.lbl_file.setProperty("hint", True)
        file_row.addWidget(self.lbl_file, 1)
        v.addLayout(file_row)

        opt_row = QHBoxLayout()
        opt_row.setContentsMargins(0, 0, 0, 0)
        self.chk_stock = QCheckBox("按文件中的数量入库")
        self.chk_stock.setChecked(True)
        self.chk_stock.setToolTip(
            "勾选：数量 > 0 的行会入库（同批次自动合并）。\n"
            "不勾：只建器件档案，数量一律忽略。"
        )
        opt_row.addWidget(self.chk_stock)
        opt_row.addStretch(1)
        self.btn_import = QPushButton("开始导入")
        self.btn_import.setProperty("accent", True)
        self.btn_import.setEnabled(False)
        self.btn_import.clicked.connect(self.run_import)
        opt_row.addWidget(self.btn_import)
        v.addLayout(opt_row)

        self.lbl_plan = QLabel("选择文件后，这里会先列出「导入后会发生什么」，确认无误再点「开始导入」。")
        self.lbl_plan.setProperty("hint", True)
        self.lbl_plan.setWordWrap(True)
        v.addWidget(self.lbl_plan)

        v.addWidget(QLabel("导入结果"))
        self.txt_report = QPlainTextEdit()
        self.txt_report.setReadOnly(True)
        self.txt_report.setPlaceholderText("导入完成后，结果明细会显示在这里。")
        v.addWidget(self.txt_report, 1)

    # ------------------------------------------------------------------
    #  选文件与预演
    # ------------------------------------------------------------------

    def _ask_open_path(self) -> str:
        """弹选择文件对话框。同样抽出来给测试替换（理由见导出面板的 _ask_save_path）。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择器件清单", _start_dir(),
            "器件清单 (*.csv *.xlsx *.xlsm);;所有文件 (*)",
        )
        return path

    def choose_file(self) -> None:
        path = self._ask_open_path()
        if path:
            self.load(path)

    def load(self, path: str | Path) -> None:
        """读文件并做一次只读预演（不落库）。"""
        path = Path(path)
        try:
            rows, errors = read_import_rows(path)
            plan = plan_import(self.svc, rows, errors)
        except Exception as exc:  # noqa: BLE001
            log.exception("读取清单失败：%s", path)
            QMessageBox.critical(self, "读取失败", f"这份文件读不出来：\n\n{exc}")
            return

        self._path = path
        self._rows = rows
        self._parse_errors = errors
        config.set_value("bom_dir", str(path.parent))

        self.lbl_file.setText(path.name)
        self.lbl_plan.setText(self._describe_plan(plan))
        self.btn_import.setEnabled(bool(rows))

    def _describe_plan(self, plan: ImportPlan) -> str:
        lines = []
        bad = len(plan.errors)
        lines.append(
            f"有效 {len(plan.rows)} 行" + (f"，另有 {bad} 行有问题（导入时跳过）" if bad else "")
        )
        if plan.new_parts or plan.existing_parts:
            lines.append(
                f"器件：新建 {plan.new_parts} 个；已存在 {plan.existing_parts} 个"
                f"（不改它们的档案）"
            )
        if plan.stock_lines:
            lines.append(f"库存：{plan.stock_lines} 行将入库，合计 {plan.stock_qty} 颗")
        else:
            lines.append("库存：文件里没有数量 > 0 的行，只建档案")
        auto = []
        if plan.new_categories:
            auto.append(f"分类 {len(plan.new_categories)} 个")
        if plan.new_locations:
            auto.append(f"位置 {len(plan.new_locations)} 个")
        if auto:
            lines.append("将自动创建：" + "、".join(auto))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  执行
    # ------------------------------------------------------------------

    def run_import(self) -> None:
        if not self._rows or self._path is None:
            return

        with_stock = self.chk_stock.isChecked()
        plan = plan_import(self.svc, self._rows)

        action = []
        if plan.new_parts:
            action.append(f"· 新建 {plan.new_parts} 个器件档案")
        if plan.existing_parts:
            action.append(f"· {plan.existing_parts} 个已存在的器件沿用原档案")
        if with_stock and plan.stock_lines:
            action.append(f"· 入库 {plan.stock_lines} 行，合计 {plan.stock_qty} 颗")
        elif plan.stock_lines:
            action.append(f"· 按开关跳过 {plan.stock_lines} 行的入库（只建档）")
        if plan.new_categories:
            action.append("· 自动创建分类：" + "、".join(plan.new_categories[:3])
                          + ("…" if len(plan.new_categories) > 3 else ""))
        if plan.new_locations:
            action.append("· 自动创建位置：" + "、".join(plan.new_locations[:3])
                          + ("…" if len(plan.new_locations) > 3 else ""))
        if self._parse_errors:
            action.append(f"· 注意：另有 {len(self._parse_errors)} 行有问题，导入时会跳过")

        confirm = QMessageBox.question(
            self, "确认导入",
            f"即将导入「{self._path.name}」：\n\n" + "\n".join(action) + "\n\n"
            f"提示：同一份文件重复导入会重复累计库存，"
            f"请确认这份文件之前没有导入过。\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        report = execute_import(self.svc, self._rows, with_stock=with_stock)

        # 报告：解析阶段的问题 + 执行阶段的问题合并展示
        errors = list(self._parse_errors) + list(report.errors)
        self.txt_report.setPlainText(self._format_report(report, errors))

        # 导入完就重置文件状态 —— 防止手一抖再把同一份文件导一遍
        self._path = None
        self._rows = []
        self._parse_errors = []
        self.lbl_file.setText("（已导入，如需再导请重新选择文件）")
        self.lbl_plan.setText("")
        self.btn_import.setEnabled(False)

        self.imported.emit()
        self._show_summary(report, errors)

    def _format_report(self, report: ImportReport, errors: list[str]) -> str:
        lines = [
            "── 导入结果 ──",
            f"有效行 {report.total_lines}",
            f"器件：新建 {report.created_parts} 个；已存在 {report.existing_parts} 个（档案未改动）",
        ]
        if report.skipped_stock_lines and not report.lots_in:
            lines.append(f"库存：按开关关闭，跳过 {report.skipped_stock_lines} 行")
        else:
            lines.append(f"库存：入库 {report.lots_in} 行，合计 {report.qty_in} 颗")
        if report.categories_created:
            lines.append("自动创建的分类：" + "、".join(report.categories_created))
        if report.locations_created:
            lines.append("自动创建的位置：" + "、".join(report.locations_created))
        if errors:
            lines.append("")
            lines.append(f"未导入 / 失败的 {len(errors)} 行：")
            lines.extend(f"  {e}" for e in errors)
        return "\n".join(lines)

    def _show_summary(self, report: ImportReport, errors: list[str]) -> None:
        head = f"新建 {report.created_parts} 个器件，入库 {report.lots_in} 行、{report.qty_in} 颗。"
        tail = "\n\n详细结果在页签下方，有问题的行按行号逐条列出。"
        if errors and report.created_parts == 0 and report.lots_in == 0:
            QMessageBox.warning(
                self, "没有导入任何内容",
                "所有行都有问题，一行都没导进去。\n\n"
                "详细原因见页签下方的「导入结果」。",
            )
        elif errors:
            QMessageBox.warning(
                self, "导入完成（有跳过的行）",
                f"{head}\n有 {len(errors)} 行没能导入。" + tail,
            )
        else:
            QMessageBox.information(self, "导入完成", head + tail)
