"""全局热键唤起的快速查询浮窗。

这是整个应用最重要的一个界面。真实使用节奏是：
画板子时一天要查几十次，而录入一周只有几次。
所以这个窗口必须做到「按下热键 → 打字 → 一眼看到结果 → 消失」，
全程不碰鼠标、不切换窗口、不需要等程序启动。

几个刻意为之的细节：
  · 无边框、置顶、不进任务栏（Qt.Tool）—— 像 Spotlight 一样浮在 CAD 上面。
  · 失焦自动隐藏 —— 点一下别处就收起来，不占地方。
  · 搜索有 80ms 防抖 —— 打字过程中不重复查库，但快到感觉不出来。
  · 结果默认 20 条 —— 足够定位，不做无意义的大列表。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.domain import InsufficientStockError, Services
from app.ui import theme

log = logging.getLogger("edms.ui.quick_search")

MAX_RESULTS = 20


class QuickSearchWindow(QWidget):
    open_part_requested = Signal(int)

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = services
        # 无边框窗口本来不需要标题，但设一个能让自动化截图/辅助工具按标题定位到它
        self.setWindowTitle("快速查询")
        # objectName 让 theme.py 里的样式表能选中这个顶层窗口，
        # 否则它的底色会走系统调色板，和内部的输入框/列表对不上
        self.setObjectName("quickWindow")

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        self.setFixedSize(680, 380)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 输入框 ----
        self.ed_input = QLineEdit()
        self.ed_input.setPlaceholderText("输入型号 / 阻值 / 封装……  例如：10k 0603")
        self.ed_input.setMinimumHeight(46)
        # 样式写在 theme.py 里（按 objectName 匹配），不在这里内联 ——
        # 内联的话切换深浅色时这份样式不会跟着更新。
        self.ed_input.setObjectName("quickInput")
        self.ed_input.textChanged.connect(self._on_text_changed)
        self.ed_input.returnPressed.connect(self._on_enter)
        root.addWidget(self.ed_input)

        # ---- 结果列表 ----
        self.tree = QTreeWidget()
        self.tree.setObjectName("quickResults")
        self.tree.setColumnCount(3)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 320)
        self.tree.setColumnWidth(1, 78)
        self.tree.setColumnWidth(2, 250)
        self.tree.itemActivated.connect(lambda _i, _c: self._on_enter())
        self.tree.itemClicked.connect(lambda _i, _c: self._on_enter())
        root.addWidget(self.tree, 1)

        # ---- 底部提示 ----
        bottom = QWidget()
        bottom.setObjectName("quickBottom")
        bh = QHBoxLayout(bottom)
        bh.setContentsMargins(12, 4, 12, 6)
        bh.setSpacing(12)

        self.lbl_hint = QLabel("↑↓ 选择　Enter 打开　Ctrl+Enter 出库 1 个　Esc 关闭")
        self.lbl_hint.setObjectName("quickHint")
        bh.addWidget(self.lbl_hint)
        bh.addStretch(1)

        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("quickCount")
        bh.addWidget(self.lbl_count)

        root.addWidget(bottom)

        # 搜索防抖
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._refresh)

        # 失焦自动隐藏
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(150)
        self._hide_timer.timeout.connect(self._hide_if_unfocused)

        self._results = []

    # ==================================================================
    #  显隐
    # ==================================================================

    def popup(self) -> None:
        """被热键唤起：清空输入、定位到鼠标所在屏幕的上方居中、抢焦点。"""
        self.ed_input.clear()
        self.tree.clear()
        self._results = []
        self.lbl_count.setText("")

        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + int(geo.height() * 0.18)   # 略偏上，视线更舒服
        self.move(max(geo.x(), x), max(geo.y(), y))

        self.show()
        self.raise_()
        self.activateWindow()
        self.ed_input.setFocus()
        self._refresh()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.popup()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        # 延迟一点再判断，避免"点击列表项"这种正常交互把自己关掉
        self._hide_timer.start()

    def _hide_if_unfocused(self) -> None:
        if not self.isActiveWindow() and self.isVisible():
            self.hide()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key_Escape:
            self.hide()
            return
        if key in (Qt.Key_Down, Qt.Key_Up):
            self._move_selection(1 if key == Qt.Key_Down else -1)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ControlModifier:
                self._quick_withdraw()
            else:
                self._on_enter()
            return
        super().keyPressEvent(event)

    # ==================================================================
    #  搜索
    # ==================================================================

    def _on_text_changed(self, _text: str) -> None:
        self._timer.start()

    def _refresh(self) -> None:
        keyword = self.ed_input.text().strip()
        self._results = self.svc.search.quick(keyword, limit=MAX_RESULTS)
        p = theme.pal()          # 每次刷新都取当前配色，切换深浅色后不长旧颜色

        self.tree.clear()
        for item in self._results:
            node = QTreeWidgetItem()
            node.setText(0, item.name)
            node.setText(1, f"{item.total_qty}")
            node.setText(2, item.location_summary)
            node.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)

            if item.is_out_of_stock:
                colour = QColor(p.fg_out)
                node.setForeground(1, colour)
                node.setForeground(2, colour)
                node.setBackground(0, QColor(p.bg_out))
                node.setBackground(1, QColor(p.bg_out))
                node.setBackground(2, QColor(p.bg_out))
                font = QFont()
                font.setBold(True)
                node.setFont(1, font)
            elif item.is_low_stock:
                colour = QColor(p.fg_low)
                node.setForeground(1, colour)
                node.setBackground(0, QColor(p.bg_low))
                node.setBackground(1, QColor(p.bg_low))
                node.setBackground(2, QColor(p.bg_low))

            self.tree.addTopLevelItem(node)

        if self._results:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            suffix = ""
            if len(self._results) >= MAX_RESULTS:
                suffix = "（只显示前 20 条，用主窗口看全部）"
            self.lbl_count.setText(f"{len(self._results)} 条{suffix}")
        else:
            self.lbl_count.setText("无匹配" if keyword else "输入关键词开始搜索")

    def _move_selection(self, delta: int) -> None:
        count = self.tree.topLevelItemCount()
        if count == 0:
            return
        current = self.tree.indexOfTopLevelItem(self.tree.currentItem())
        new_index = max(0, min(count - 1, current + delta))
        self.tree.setCurrentItem(self.tree.topLevelItem(new_index))

    # ==================================================================
    #  动作
    # ==================================================================

    def _current_part_id(self) -> int | None:
        node = self.tree.currentItem()
        if node is None:
            return None
        index = self.tree.indexOfTopLevelItem(node)
        if 0 <= index < len(self._results):
            return self._results[index].id
        return None

    def _on_enter(self) -> None:
        part_id = self._current_part_id()
        if part_id is None:
            return
        self.hide()
        self.open_part_requested.emit(part_id)

    def _quick_withdraw(self) -> None:
        """快捷出库 1 个。结果直接刷新在列表里，不用开别的窗口。"""
        part_id = self._current_part_id()
        if part_id is None:
            return
        try:
            self.svc.stock.withdraw(part_id, 1, ref="热键快速出库")
        except InsufficientStockError:
            self.lbl_count.setText("⊘ 库存为 0，无法出库")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("快速出库失败")
            self.lbl_count.setText(f"出库失败：{exc}")
            return

        # 记住当前选中的行，刷新后回到原位
        row = self.tree.indexOfTopLevelItem(self.tree.currentItem())
        self._refresh()
        if 0 <= row < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(row))
        self.lbl_count.setText(self.lbl_count.text() + "　· 已出库 1 个")
