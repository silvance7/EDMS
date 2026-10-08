"""分类管理：左栏一级分类 / 右栏该分类下的器件清单。

用户拍板的口径（2026-10-08 修订）：
    · 左栏是「一级分类」（大类，如：电阻）—— 大标题，保持不变；
    · 右栏**不是**子分类管理，而是选中一级分类后，列出挂在它（含其
      子分类）下面的所有器件 —— 用户的心智模型里「二级」就是器件本身
      （0805电阻 / 0603电阻……），不是又一层分类；
    · 删除分类是安全的（界面上必须写清）：子分类连带删，**器件不删**，
      只是变成「未分类」；
    · 需要建子分类时走主窗口分类树右键「新建子分类」—— 本页不再提供
      子分类的新建 / 重命名 / 删除入口。

同一个组件被两处复用：
    · 设置面板「分类管理」页签（内嵌 CategoryManagerWidget）
    · 器件表单里分类下拉旁的「…」（薄壳 CategoryManagerDialog）

这个模块刻意不引用 stock_dialog / part_editor —— 那两个都直接或间接
依赖本模块，反向 import 会成环。器件的双击编辑通过 `part_activated`
信号交给宿主处理（settings_dialog 自己 import 编辑器，环不在这里闭合）。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.domain import Services
from app.ui.dialog_base import EnterSafeDialog

log = logging.getLogger("edms.ui.category_manager")

ROLE_CAT_ID = Qt.UserRole + 1
ROLE_PART_ID = Qt.UserRole + 2


def _friendly_tree_error(exc: Exception) -> str:
    """把数据库约束报错翻译成人话。

    （stock_dialog 里有一份同样的 —— 两边不做互相 import，各自留住。）
    """
    text = str(exc)
    if "ux_category_name" in text or "ux_location_name" in text:
        return "这个名字在同级里已经有了，换一个吧。"
    if "ux_location_code" in text:
        return "这个短码已经被别的位置用了。"
    return text


class CategoryManagerWidget(QWidget):
    """左栏一级分类 / 右栏该分类下的器件清单。分类实际改动后发 `changed` 信号。"""

    changed = Signal()
    # 双击右栏里的某个器件行时发出（part_id）。编辑器的打开由宿主负责：
    # 本模块不能 import part_editor（会成环，见模块 docstring）。
    part_activated = Signal(int)
    # 点「＋ 添加器件到这个分类」时发出（分类 id）。同样由宿主打开
    # 器件表单并预选该分类 —— 添加"二级"就是添加器件，没有别的含义。
    add_part_requested = Signal(int)

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.svc = services

        root = QVBoxLayout(self)
        root.setSpacing(8)

        cols = QHBoxLayout()
        cols.setSpacing(12)

        # ---- 左：一级分类 ----
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(QLabel("一级分类（大类，如：电阻）"))
        self.list_l1 = QListWidget()
        self.list_l1.setMinimumWidth(230)
        self.list_l1.currentItemChanged.connect(lambda *_: self._reload_right())
        left.addWidget(self.list_l1, 1)
        row_l1 = QHBoxLayout()
        row_l1.setSpacing(6)
        btn_l1_add = QPushButton("＋ 新建一级")
        btn_l1_add.clicked.connect(lambda: self._add_l1())
        row_l1.addWidget(btn_l1_add)
        btn_l1_ren = QPushButton("重命名")
        btn_l1_ren.clicked.connect(lambda: self._rename_l1())
        row_l1.addWidget(btn_l1_ren)
        btn_l1_del = QPushButton("删除")
        btn_l1_del.setProperty("danger", True)
        btn_l1_del.clicked.connect(lambda: self._delete_l1())
        row_l1.addWidget(btn_l1_del)
        left.addLayout(row_l1)
        cols.addLayout(left, 1)

        # ---- 右：选中一级分类后，列出挂在它下面的器件 ----
        right = QVBoxLayout()
        right.setSpacing(6)
        self.lbl_right = QLabel("器件清单（先选左边的一级分类）")
        self.lbl_right.setWordWrap(True)
        right.addWidget(self.lbl_right)
        self.list_parts = QListWidget()
        self.list_parts.setMinimumWidth(230)
        self.list_parts.setWordWrap(False)
        self.list_parts.itemDoubleClicked.connect(self._on_part_double_clicked)
        right.addWidget(self.list_parts, 1)
        row_right = QHBoxLayout()
        row_right.setSpacing(6)
        btn_add_part = QPushButton("＋ 添加器件到这个分类")
        btn_add_part.setToolTip(
            "打开器件表单，分类自动选成左边选中的大类。\n"
            "「添加二级」就是添加器件 —— 器件建好就会列在上面。")
        btn_add_part.clicked.connect(lambda: self._add_part_to_current())
        row_right.addWidget(btn_add_part)
        row_right.addStretch(1)
        right.addLayout(row_right)
        cols.addLayout(right, 1)

        root.addLayout(cols, 1)

        self.lbl_hint = QLabel(
            "右边列出的是挂在所选分类（含它的子分类）下面的器件，"
            "双击可以直接编辑。\n"
            "删除分类不会删器件：挂在它下面的器件只是变成「未分类」，"
            "主窗口树上的「未分类」节点里能找到它们。"
        )
        self.lbl_hint.setProperty("hint", True)
        self.lbl_hint.setWordWrap(True)
        root.addWidget(self.lbl_hint)

        self.reload()

    # ==================================================================

    def reload(self) -> None:
        """重填两栏（尽量保住当前选中的一级分类）。"""
        keep = self._current_l1_id()
        counts = self._part_counts()

        self.list_l1.blockSignals(True)
        self.list_l1.clear()
        for cat in sorted((c for c in self.svc.tree.list_categories()
                           if c.parent_id is None), key=lambda c: c.name.lower()):
            item = QListWidgetItem(f"{cat.name}（{counts.get(cat.id, 0)}）")
            item.setData(ROLE_CAT_ID, cat.id)
            self.list_l1.addItem(item)
        self.list_l1.blockSignals(False)

        if keep is not None:
            for i in range(self.list_l1.count()):
                if self.list_l1.item(i).data(ROLE_CAT_ID) == keep:
                    self.list_l1.setCurrentRow(i)
                    break
        if self.list_l1.currentRow() < 0 and self.list_l1.count():
            self.list_l1.setCurrentRow(0)
        self._reload_right()

    def _reload_right(self) -> None:
        """右栏：列出挂在该一级分类（含其子分类）下的所有器件。"""
        l1_id = self._current_l1_id()
        self.list_parts.clear()
        if l1_id is None:
            self.lbl_right.setText("器件清单（先选左边的一级分类）")
            self._empty_hint("还没选分类 —— 在左边点一个一级分类。")
            return

        name = self.svc.tree.category_path(l1_id) or str(l1_id)
        rows = self._parts_in_category(l1_id)
        self.lbl_right.setText(f"「{name}」下的器件（{len(rows)}）")
        if not rows:
            # 空态说清楚为什么是空的、去哪儿建器件 —— 不让用户猜。
            # QListWidget 没有 placeholderText，用一条不可选中的灰行兜底。
            self._empty_hint(
                "这个分类下还没有器件。\n"
                "点下面「＋ 添加器件到这个分类」建一个，建好就会出现在这里。")
            return

        # 子分类挂载的器件标注一下路径，和直接挂一级的区分开
        l1_name = name.split(" / ")[-1]
        for r in rows:
            label = f"{r['name']}（{r['total']}）"
            if r["category_id"] != l1_id:
                sub = self._category_short_path(r["category_id"], l1_name)
                if sub:
                    label += f" · {sub}"
            item = QListWidgetItem(label)
            item.setData(ROLE_PART_ID, int(r["id"]))
            self.list_parts.addItem(item)

    def _empty_hint(self, text: str) -> None:
        """空态占位行：NoItemFlags 不可选中，双击路径也不会带出器件 id。"""
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)
        self.list_parts.addItem(item)

    # ==================================================================
    #  内部工具
    # ==================================================================

    def _current_l1_id(self) -> int | None:
        item = self.list_l1.currentItem()
        return item.data(ROLE_CAT_ID) if item is not None else None

    @staticmethod
    def _plain_name(text: str) -> str:
        """列表项文本是「名字（N）」，取回纯名字做重命名默认值。"""
        return text.rsplit("（", 1)[0].strip()

    def _category_map(self) -> dict[int, tuple[str, int | None]]:
        """id -> (name, parent_id)，一次查全表，避免循环里逐行查库。"""
        rows = self.svc.db.query("SELECT id, name, parent_id FROM category")
        return {r["id"]: (r["name"], r["parent_id"]) for r in rows}

    def _category_short_path(self, category_id: int, root_name: str) -> str:
        """从 category_id 往上拼到 root_name 为止的子路径，如 "0805电阻"。

        直接挂一级的器件不需要标（右栏标题已经说了是哪个大类）；
        挂在子分类上的才标，让用户知道它其实归在哪个细分下。
        """
        cmap = self._category_map()
        names: list[str] = []
        cur = category_id
        for _ in range(32):
            if cur is None or cur not in cmap:
                break
            cat_name, parent = cmap[cur]
            if cat_name == root_name:
                break
            names.append(cat_name)
            cur = parent
        return " / ".join(reversed(names))

    def _parts_in_category(self, l1_id: int) -> list:
        """挂在 l1_id（含全部子孙分类）下的器件 + 总库存。一次查询拿齐。"""
        ids = self.svc.tree.category_ids_with_children(l1_id)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.svc.db.query(
            f"""
            SELECT p.id, p.name, p.category_id,
                   COALESCE(SUM(sl.quantity), 0) AS total
            FROM part p
            LEFT JOIN stock_lot sl ON sl.part_id = p.id
            WHERE p.category_id IN ({marks})
            GROUP BY p.id
            ORDER BY p.name COLLATE NOCASE
            """,
            ids,
        )

    def _part_counts(self) -> dict[int, int]:
        """左栏每个一级分类的器件数 —— 口径与右栏一致：**含子孙分类**。

        先按 category_id 拿直接计数，再沿树把子分类的数量累加给祖先。
        （用户看到的左栏数字必须等于右栏列出的行数，两个口径不一致
        会再次变成"看着像 bug 的误解"。）
        """
        direct: dict[int, int] = {}
        for r in self.svc.db.query(
            "SELECT category_id, COUNT(*) AS n FROM part "
            "WHERE category_id IS NOT NULL GROUP BY category_id"
        ):
            direct[r["category_id"]] = r["n"]

        cmap = self._category_map()
        total: dict[int, int] = {}
        for cat_id in cmap:
            cur = cat_id
            for _ in range(32):
                if cur is None or cur not in cmap:
                    break
                total[cur] = total.get(cur, 0) + direct.get(cat_id, 0)
                cur = cmap[cur][1]
        return total

    def _on_part_double_clicked(self, item: QListWidgetItem) -> None:
        part_id = item.data(ROLE_PART_ID)
        if part_id is not None:
            self.part_activated.emit(int(part_id))

    def _add_part_to_current(self) -> None:
        """「＋ 添加器件到这个分类」：让宿主开器件表单并预选当前大类。"""
        l1_id = self._current_l1_id()
        if l1_id is None:
            QMessageBox.information(
                self, "先选分类", "先在左边选一个一级分类，再往它下面添加器件。")
            return
        self.add_part_requested.emit(int(l1_id))

    def _ask_name(self, title: str, prompt: str, initial: str = "") -> str | None:
        text, ok = QInputDialog.getText(self, title, prompt, text=initial)
        if not ok:
            return None
        text = text.strip()
        if not text:
            QMessageBox.information(self, "先填名称", "名字不能是空的。")
            return None
        return text

    def _create(self, name: str, parent_id: int | None) -> None:
        try:
            self.svc.tree.create_category(name, parent_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("新增分类失败")
            QMessageBox.critical(self, "新增失败", _friendly_tree_error(exc))
            return
        self.changed.emit()
        self.reload()

    def _rename(self, cat_id: int, current: str) -> None:
        name = self._ask_name("重命名分类", "新名称：", initial=current)
        if name is None:
            return
        try:
            self.svc.tree.rename_category(cat_id, name)
        except Exception as exc:  # noqa: BLE001
            log.exception("重命名分类失败")
            QMessageBox.critical(self, "重命名失败", _friendly_tree_error(exc))
            return
        self.changed.emit()
        self.reload()

    def _delete(self, cat_id: int) -> None:
        ids = self.svc.tree.category_ids_with_children(cat_id)
        marks = ",".join("?" * len(ids))
        n_parts = self.svc.db.query_one(
            f"SELECT COUNT(*) AS n FROM part WHERE category_id IN ({marks})", ids)["n"]
        name = self.svc.tree.category_path(cat_id) or str(cat_id)
        if QMessageBox.question(
            self, "确认删除",
            f"删除「{name}」？\n\n"
            f"· 它下面的子分类会一起删\n"
            f"· 挂在它（含子分类）下面的 {n_parts} 个器件**不会被删**，"
            f"只是变成「未分类」\n\n继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            self.svc.tree.delete_category(cat_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("删除分类失败")
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        self.changed.emit()
        self.reload()

    # ==================================================================
    #  按钮（全部针对左栏选中的一级分类）
    # ==================================================================

    def _add_l1(self) -> None:
        name = self._ask_name("新建一级分类", "名称（大类，如：电阻）：")
        if name is not None:
            self._create(name, None)

    def _rename_l1(self) -> None:
        item = self.list_l1.currentItem()
        if item is None:
            QMessageBox.information(self, "先选中", "先在左边选一个一级分类。")
            return
        self._rename(item.data(ROLE_CAT_ID), self._plain_name(item.text()))

    def _delete_l1(self) -> None:
        item = self.list_l1.currentItem()
        if item is None:
            QMessageBox.information(self, "先选中", "先在左边选一个一级分类。")
            return
        self._delete(item.data(ROLE_CAT_ID))


class CategoryManagerDialog(EnterSafeDialog):
    """薄壳：器件表单里分类「…」用的对话框，内嵌一个管理组件。"""

    def __init__(self, services: Services, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.changed = False

        self.setWindowTitle("分类管理")
        self.setMinimumSize(640, 460)

        root = QVBoxLayout(self)
        self.manager = CategoryManagerWidget(services)
        self.manager.changed.connect(self._on_changed)
        root.addWidget(self.manager, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_changed(self) -> None:
        self.changed = True
