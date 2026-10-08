"""设置面板：快捷键 / 存储位置 / 分类管理。

入口：主窗口左侧边栏的 ⚙、托盘右键菜单「设置…」。

两个页签：
    常规     —— 全局热键（唤起浮窗，可改）、应用内快捷键（主窗口三个动作）、
               数据库 / 日志的存放位置（一键打开文件夹）、主题与托盘气泡
    分类管理 —— 左栏一级分类（增 / 重命名 / 删），右栏列出该分类下的器件
               （双击编辑、一键添加器件到该分类）

原「器件资料」页签已于 2026-10-08 删除：主窗口已经完整覆盖器件的
新建 / 编辑 / 删除（表格右键、双击、详情面板），设置里这份是冗余入口。
器件表单本身（PartEditorDialog）仍在用 —— 分类管理页签与入库面板都调它。

对话框上改动的所有东西**立即生效**（热键当场换绑、主题当场切换）；
关掉之后主窗口按 `parts_changed` 决定要不要整体刷新。

全局热键的换绑能力由 Controller 通过 `set_hotkey_hooks` 注入 ——
没注入（比如单测直接建窗口）时，热键区退化成只读显示，不假装能改。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app import config
from app.domain import Services
from app.paths import log_dir
from app.ui import theme
from app.ui.category_manager import CategoryManagerWidget
from app.ui.dialog_base import EnterSafeDialog
from app.ui.hotkey import validate_hotkey
from app.ui.part_editor import PartEditorDialog, build_category_labels

log = logging.getLogger("edms.ui.settings_dialog")

# Qt 的 PortableText 键名 -> parse_hotkey 认得的名字
_QT_KEY_ALIASES = {
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdown": "pagedown",
    "esc": "escape",
    "return": "enter",
    "meta": "win",
    "cmd": "win",
}

_INAPP_DEFAULTS = {
    "shortcut_new_part": "Ctrl+N",
    "shortcut_focus_search": "Ctrl+F",
    "shortcut_delete_part": "Del",
}


def qt_sequence_to_spec(seq: QKeySequence) -> str:
    """QKeySequence 的 PortableText -> 本程序的 "ctrl+alt+q" 风格描述。"""
    text = seq.toString(QKeySequence.PortableText)
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    return "+".join(_QT_KEY_ALIASES.get(p, p) for p in parts)


class SettingsDialog(EnterSafeDialog):
    def __init__(self, services: Services, parent: QWidget | None = None,
                 hotkey_apply=None, hotkey_current=None) -> None:
        super().__init__(parent)
        self.svc = services
        self.parts_changed = False          # 关闭后主窗口据此整体刷新
        self._hotkey_apply = hotkey_apply   # (spec) -> (ok, msg)；Controller 注入
        self._hotkey_current = hotkey_current

        self.setWindowTitle("设置")
        self.setMinimumSize(680, 560)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_general_tab(), "常规")
        self.tabs.addTab(self._build_category_tab(), "分类管理")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ==================================================================
    #  页签一：常规
    # ==================================================================

    def _build_general_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(10)

        # ---------------- 全局热键 ----------------
        gb_hotkey = QGroupBox("全局热键（唤起快速查询浮窗）")
        hk = QVBoxLayout(gb_hotkey)

        self.lbl_hotkey_now = QLabel("")
        hk.addWidget(self.lbl_hotkey_now)

        hk_row = QHBoxLayout()
        self.ed_hotkey = QKeySequenceEdit()
        self.ed_hotkey.setMaximumSequenceLength(1)
        self.ed_hotkey.setToolTip("点一下输入框，直接按想要的组合键（如 Ctrl+Alt+Q）")
        hk_row.addWidget(self.ed_hotkey, 1)

        self.btn_apply_hotkey = QPushButton("应用")
        self.btn_apply_hotkey.setProperty("accent", True)
        self.btn_apply_hotkey.clicked.connect(lambda: self._apply_hotkey())
        hk_row.addWidget(self.btn_apply_hotkey)

        self.btn_reset_hotkey = QPushButton("恢复默认")
        self.btn_reset_hotkey.setToolTip("回到默认的 Ctrl + Alt + E")
        self.btn_reset_hotkey.clicked.connect(lambda: self._reset_hotkey())
        hk_row.addWidget(self.btn_reset_hotkey)
        hk.addLayout(hk_row)

        self.lbl_hotkey_status = QLabel("")
        self.lbl_hotkey_status.setProperty("hint", True)
        self.lbl_hotkey_status.setWordWrap(True)
        hk.addWidget(self.lbl_hotkey_status)

        hk_hint = QLabel(
            "至少带一个修饰键（Ctrl / Alt / Shift / Win）。被其它程序占用时"
            "换绑会失败并保留原热键 —— 换一个组合再试。")
        hk_hint.setProperty("hint", True)
        hk_hint.setWordWrap(True)
        hk.addWidget(hk_hint)
        v.addWidget(gb_hotkey)

        # ---------------- 应用内快捷键 ----------------
        gb_app = QGroupBox("应用内快捷键（主窗口）")
        app_v = QVBoxLayout(gb_app)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.ed_sc_new = QKeySequenceEdit()
        self.ed_sc_new.setMaximumSequenceLength(1)
        form.addRow("新建器件", self.ed_sc_new)

        self.ed_sc_focus = QKeySequenceEdit()
        self.ed_sc_focus.setMaximumSequenceLength(1)
        form.addRow("聚焦搜索", self.ed_sc_focus)

        self.ed_sc_del = QKeySequenceEdit()
        self.ed_sc_del.setMaximumSequenceLength(1)
        form.addRow("删除器件", self.ed_sc_del)

        app_v.addLayout(form)

        sc_row = QHBoxLayout()
        btn_apply_sc = QPushButton("应用")
        btn_apply_sc.clicked.connect(lambda: self._apply_inapp_shortcuts())
        sc_row.addWidget(btn_apply_sc)
        btn_reset_sc = QPushButton("恢复默认")
        btn_reset_sc.clicked.connect(lambda: self._reset_inapp_shortcuts())
        sc_row.addWidget(btn_reset_sc)
        sc_row.addStretch(1)
        app_v.addLayout(sc_row)

        self.lbl_sc_status = QLabel("")
        self.lbl_sc_status.setProperty("hint", True)
        self.lbl_sc_status.setWordWrap(True)
        app_v.addWidget(self.lbl_sc_status)
        v.addWidget(gb_app)

        # ---------------- 存储位置 ----------------
        gb_paths = QGroupBox("存储位置")
        paths_form = QFormLayout(gb_paths)
        paths_form.setLabelAlignment(Qt.AlignRight)

        self.ed_db_dir = QLineEdit(str(self.svc.db.db_path.parent))
        self.ed_db_dir.setReadOnly(True)
        self.ed_db_dir.setToolTip("inventory.db 与 settings.json 都在这个目录里")
        paths_form.addRow("数据库存放地址",
                          self._path_row(self.ed_db_dir, self.svc.db.db_path.parent))

        self.ed_log_dir = QLineEdit(str(log_dir()))
        self.ed_log_dir.setReadOnly(True)
        self.ed_log_dir.setToolTip(
            "按天分文件：info.2026-10-08.log / error.2026-10-08.log，各留最近 10 天")
        paths_form.addRow("日志存放位置", self._path_row(self.ed_log_dir, log_dir()))
        v.addWidget(gb_paths)

        # ---------------- 其它（顺手补上的既有配置项） ----------------
        gb_misc = QGroupBox("其它")
        misc_form = QFormLayout(gb_misc)
        misc_form.setLabelAlignment(Qt.AlignRight)

        self.cb_theme = QComboBox()
        for label, value in (("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark")):
            self.cb_theme.addItem(label, value)
        idx = self.cb_theme.findData((config.get("theme") or "auto").lower())
        self.cb_theme.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_theme.currentIndexChanged.connect(lambda _i: self._on_theme_changed())
        misc_form.addRow("主题", self.cb_theme)

        self.chk_notify = QCheckBox("托盘气泡通知")
        self.chk_notify.setChecked(bool(config.get("show_tray_notifications")))
        self.chk_notify.stateChanged.connect(
            lambda _s: config.set_value(
                "show_tray_notifications", self.chk_notify.isChecked()))
        misc_form.addRow("", self.chk_notify)
        v.addWidget(gb_misc)

        v.addStretch(1)

        # 勾住滚动：小屏 + 125% 缩放时这一页会超出高度
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)

        self._refresh_hotkey_display()
        self._reload_inapp_editors()
        return scroll

    def _path_row(self, edit: QLineEdit, path) -> QWidget:
        """只读路径 + 「打开文件夹」按钮。"""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(edit, 1)
        btn = QPushButton("打开文件夹")
        btn.setToolTip("在资源管理器里打开这个位置")
        # 包 lambda：clicked 带 bool
        btn.clicked.connect(lambda: self._open_folder(path))
        h.addWidget(btn)
        return row

    def _open_folder(self, path) -> None:
        """打开文件夹。

        单独一个实例方法是有意的：测试覆写它即可 —— **不要**去 patch
        QDesktopServices.openUrl 这类 Qt 静态方法（PySide6 里替换不生效，
        和 QMenu.exec 是同一类坑）。
        """
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception:  # noqa: BLE001
            log.exception("打开文件夹失败：%s", path)

    # ---------------- 全局热键 ----------------

    def _refresh_hotkey_display(self) -> None:
        hooked = callable(self._hotkey_apply) and callable(self._hotkey_current)
        if hooked:
            label = self._hotkey_current() or "—"
            self.lbl_hotkey_now.setText(f"当前生效：<b>{label}</b>")
            self.btn_apply_hotkey.setEnabled(True)
            self.btn_reset_hotkey.setEnabled(True)
        else:
            label = (config.get("hotkey") or "").replace("+", " + ").upper()
            self.lbl_hotkey_now.setText(
                f"当前配置：<b>{label}</b>（现在是只读模式，改不了）")
            self.btn_apply_hotkey.setEnabled(False)
            self.btn_reset_hotkey.setEnabled(False)
            self.lbl_hotkey_status.setText("没有连接到运行中的程序，暂不能换绑。")

    def _set_hotkey_status(self, text: str, error: bool) -> None:
        color = theme.pal().fg_out if error else theme.pal().fg_ok
        self.lbl_hotkey_status.setText(text)
        self.lbl_hotkey_status.setStyleSheet(f"color: {color};")

    def _apply_hotkey(self) -> None:
        if not callable(self._hotkey_apply):
            return
        seq = self.ed_hotkey.keySequence()
        if seq.isEmpty():
            self._set_hotkey_status("先按一下想用的组合（如 Ctrl+Alt+Q），再点应用。", True)
            return
        spec = qt_sequence_to_spec(seq)
        error = validate_hotkey(spec)
        if error:
            self._set_hotkey_status(error, True)
            return
        ok, msg = self._hotkey_apply(spec)
        self._set_hotkey_status(msg, error=not ok)
        if ok:
            self.ed_hotkey.clear()
            self._refresh_hotkey_display()

    def _reset_hotkey(self) -> None:
        if not callable(self._hotkey_apply):
            return
        ok, msg = self._hotkey_apply("ctrl+alt+e")
        self._set_hotkey_status(msg, error=not ok)
        if ok:
            self.ed_hotkey.clear()
            self._refresh_hotkey_display()

    # ---------------- 应用内快捷键 ----------------

    def _reload_inapp_editors(self) -> None:
        for key, editor in (("shortcut_new_part", self.ed_sc_new),
                            ("shortcut_focus_search", self.ed_sc_focus),
                            ("shortcut_delete_part", self.ed_sc_del)):
            editor.setKeySequence(QKeySequence(config.get(key) or ""))

    def _set_sc_status(self, text: str, error: bool) -> None:
        color = theme.pal().fg_out if error else theme.pal().fg_ok
        self.lbl_sc_status.setText(text)
        self.lbl_sc_status.setStyleSheet(f"color: {color};")

    def _collect_inapp_specs(self) -> tuple[str, str, str]:
        return (
            self.ed_sc_new.keySequence().toString(QKeySequence.PortableText),
            self.ed_sc_focus.keySequence().toString(QKeySequence.PortableText),
            self.ed_sc_del.keySequence().toString(QKeySequence.PortableText),
        )

    def _apply_inapp_shortcuts(self) -> None:
        apply_fn = getattr(self.parent(), "apply_inapp_shortcuts", None)
        if not callable(apply_fn):
            self._set_sc_status("当前环境改不了（没有主窗口），这个区域仅展示。", True)
            return
        ok, msg = apply_fn(*self._collect_inapp_specs())
        self._set_sc_status(msg, error=not ok)
        if ok:
            self._reload_inapp_editors()

    def _reset_inapp_shortcuts(self) -> None:
        self.ed_sc_new.setKeySequence(QKeySequence(_INAPP_DEFAULTS["shortcut_new_part"]))
        self.ed_sc_focus.setKeySequence(QKeySequence(_INAPP_DEFAULTS["shortcut_focus_search"]))
        self.ed_sc_del.setKeySequence(QKeySequence(_INAPP_DEFAULTS["shortcut_delete_part"]))
        self._apply_inapp_shortcuts()

    # ---------------- 主题 ----------------

    def _on_theme_changed(self) -> None:
        mode = self.cb_theme.currentData() or "auto"
        app = QApplication.instance()
        if app is not None:
            theme.apply(app, mode)
        config.set_value("theme", mode)
        # 侧栏图标是 QPainter 烘死颜色的，得让主窗口重画一遍
        parent = self.parent()
        if hasattr(parent, "refresh_rail_icons"):
            parent.refresh_rail_icons()

    # ==================================================================
    #  页签二：分类管理
    # ==================================================================

    def _build_category_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self.category_manager = CategoryManagerWidget(self.svc)
        self.category_manager.changed.connect(self._on_categories_changed)
        # 右栏就是器件清单：双击编辑、按钮新建（预选当前大类）。
        # 编辑器由本页打开 —— category_manager 不 import part_editor（防成环）。
        self.category_manager.part_activated.connect(self._edit_part_from_category)
        self.category_manager.add_part_requested.connect(self._add_part_from_category)
        v.addWidget(self.category_manager, 1)
        return page

    def _edit_part_from_category(self, part_id: int) -> None:
        """右栏双击器件 —— 打开编辑表单（改完回填清单与计数）。"""
        dlg = PartEditorDialog(
            self.svc.parts,
            build_category_labels(self.svc.tree),
            part_id=part_id,
            services=self.svc,
            parent=self,
        )
        if dlg.exec():
            self._on_data_mutated()

    def _add_part_from_category(self, category_id: int) -> None:
        """「＋ 添加器件到这个分类」—— 新建表单，分类已预选好。"""
        dlg = PartEditorDialog(
            self.svc.parts,
            build_category_labels(self.svc.tree),
            default_category_id=category_id,
            services=self.svc,
            parent=self,
        )
        if dlg.exec():
            self._on_data_mutated()

    def _on_data_mutated(self) -> None:
        """器件被新建 / 改动之后：让主窗口整体刷新，分类页签的清单与计数跟上。"""
        self.parts_changed = True
        self.category_manager.reload()

    def _on_categories_changed(self) -> None:
        # 分类动了：主窗口的树 / 未分类节点要刷；分类页签自己会 reload
        self.parts_changed = True
        self.category_manager.reload()
