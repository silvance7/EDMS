"""界面主题：浅色 / 深色两套配色，默认跟随系统。

**之前那个"黑底黑字看不见"的根因**：
    我在 QWidget 上写死了深色文字（#1F1F1F），但 QComboBox 的下拉列表、
    QMenu（含托盘右键菜单）、QCheckBox 指示器这几类控件**没有写样式**。
    它们于是跟随操作系统的调色板——系统是深色，底就变成黑的，
    配上我写死的黑字，彻底看不见。

**解法是双保险，缺一不可**：
    1. 用 QPalette 把"QSS 没覆盖到的控件"整体兜住（复选框、弹出窗口等）。
       并强制用 Fusion 样式——Windows 原生样式不完全跟随调色板，
       Fusion 才是"给什么调色板就画什么颜色"。
    2. QSS 里显式补上 QMenu / QComboBox 弹出列表 / QListView 的样式，
       这些是弹出式顶层窗口，光靠调色板在个别平台上仍会走样。

库存状态的三色是**告警语义**：红=缺货、琥珀=偏低，
跟股市红涨绿跌那套约定无关。

改了配色相关的常量，务必两套主题都实跑看一眼。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

Mode = str  # "auto" | "light" | "dark"


@dataclass(frozen=True)
class Palette:
    name: str

    # 背景层次
    bg_window: str       # 窗口底色
    bg_panel: str        # 输入框 / 表格 / 列表底色
    bg_group: str        # 分组框底色
    bg_header: str       # 表头
    bg_hover: str        # 悬停
    bg_select: str       # 选中
    bg_low: str          # 低库存整行
    bg_out: str          # 缺货整行

    # 文字
    fg_normal: str
    fg_muted: str
    fg_disabled: str
    fg_on_accent: str

    # 线与语义
    border: str
    border_strong: str
    accent: str
    accent_hover: str
    accent_pressed: str
    fg_low: str
    fg_out: str

    # 成本口径的语义色（和库存告警是两回事，别混用）：
    #   fg_ok        —— 全部都有价，这套板子的钱算得全
    #   fg_attention —— 还差价格 / 有行没匹配上，算出来的钱是**不完全**的
    # 注意这跟股市红涨绿跌无关，就是"齐了 / 没齐"。
    fg_ok: str
    fg_attention: str

    # 滚动条 / 提示气泡
    scroll: str
    scroll_hover: str
    tooltip_bg: str
    tooltip_fg: str
    tooltip_border: str

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


LIGHT = Palette(
    name="light",
    bg_window="#F5F5F5",
    bg_panel="#FFFFFF",
    bg_group="#FFFFFF",
    bg_header="#F2F4F6",
    bg_hover="#EDF4FB",
    bg_select="#D6E8F9",
    bg_low="#FFF7E6",
    bg_out="#FDECEA",
    fg_normal="#1F1F1F",
    fg_muted="#6B6B6B",
    fg_disabled="#A8A8A8",
    fg_on_accent="#FFFFFF",
    border="#D8D8D8",
    border_strong="#B4B4B4",
    accent="#2F7ED8",
    accent_hover="#3B8BE4",
    accent_pressed="#2769B4",
    fg_low="#9A5B00",
    fg_out="#B3261E",
    fg_ok="#1E7A3C",
    fg_attention="#9A5B00",
    scroll="#C6C6C6",
    scroll_hover="#A8A8A8",
    tooltip_bg="#FFFDE7",
    tooltip_fg="#1F1F1F",
    tooltip_border="#D6CFA0",
)

DARK = Palette(
    name="dark",
    bg_window="#1E1E1E",
    bg_panel="#2A2A2A",
    bg_group="#252525",
    bg_header="#333333",
    bg_hover="#333F4A",
    bg_select="#2C4A66",
    bg_low="#3B2F14",
    bg_out="#43201F",
    fg_normal="#E4E4E4",
    fg_muted="#9C9C9C",
    fg_disabled="#6A6A6A",
    fg_on_accent="#FFFFFF",
    border="#3C3C3C",
    border_strong="#505050",
    accent="#3B8BE4",
    accent_hover="#4E9AEC",
    accent_pressed="#2F76C4",
    fg_low="#E8B563",
    fg_out="#FF7B72",
    fg_ok="#5FD08A",
    fg_attention="#E8B563",
    scroll="#4A4A4A",
    scroll_hover="#5E5E5E",
    tooltip_bg="#333333",
    tooltip_fg="#E4E4E4",
    tooltip_border="#4A4A4A",
)

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'

_current: Palette = LIGHT


def pal() -> Palette:
    """当前生效的配色。UI 代码取颜色一律走这里，别再写死十六进制值。"""
    return _current


# ===========================================================================
#  系统主题探测
# ===========================================================================

def system_mode() -> str:
    """读取系统当前是浅色还是深色。Qt 6.5+ 提供 colorScheme()。"""
    app = QGuiApplication.instance()
    if app is None:
        return "light"
    getter = getattr(app.styleHints(), "colorScheme", None)
    if getter is None:
        return "light"
    return "dark" if getter() == Qt.ColorScheme.Dark else "light"


def resolve_mode(mode: Mode) -> str:
    """把配置里的 auto/light/dark 解析成实际生效的 light/dark。"""
    mode = (mode or "auto").lower()
    if mode in ("light", "dark"):
        return mode
    return system_mode()


# ===========================================================================
#  调色板：兜住所有 QSS 没覆盖到的控件
# ===========================================================================

def build_palette(p: Palette) -> QPalette:
    """构造 QPalette。

    这是"黑底黑字"的第一道防线：QSS 只覆盖我显式写过选择器的控件，
    复选框、QCompleter 弹出框、文件对话框之类的原生控件全部走调色板。
    """
    qp = QPalette()
    c = QColor

    qp.setColor(QPalette.Window, c(p.bg_window))
    qp.setColor(QPalette.WindowText, c(p.fg_normal))
    qp.setColor(QPalette.Base, c(p.bg_panel))
    qp.setColor(QPalette.AlternateBase, c(p.bg_group))
    qp.setColor(QPalette.Text, c(p.fg_normal))
    qp.setColor(QPalette.PlaceholderText, c(p.fg_muted))
    qp.setColor(QPalette.Button, c(p.bg_panel))
    qp.setColor(QPalette.ButtonText, c(p.fg_normal))
    qp.setColor(QPalette.BrightText, c(p.fg_out))
    qp.setColor(QPalette.ToolTipBase, c(p.tooltip_bg))
    qp.setColor(QPalette.ToolTipText, c(p.tooltip_fg))
    qp.setColor(QPalette.Highlight, c(p.bg_select))
    qp.setColor(QPalette.HighlightedText, c(p.fg_normal))
    qp.setColor(QPalette.Link, c(p.accent))

    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        qp.setColor(QPalette.Disabled, role, c(p.fg_disabled))
    qp.setColor(QPalette.Disabled, QPalette.Highlight, c(p.bg_header))
    qp.setColor(QPalette.Disabled, QPalette.HighlightedText, c(p.fg_disabled))

    return qp


# ===========================================================================
#  样式表
# ===========================================================================

def build_qss(p: Palette) -> str:
    return f"""
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: 13px;
    color: {p.fg_normal};
}}

QMainWindow, QDialog {{ background: {p.bg_window}; }}

/* ---- 输入控件 ---- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {p.bg_panel};
    color: {p.fg_normal};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {p.bg_select};
    selection-color: {p.fg_normal};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {p.accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {p.bg_group};
    color: {p.fg_disabled};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.fg_muted};
    width: 0; height: 0;
    margin-right: 6px;
}}

/* ---- 弹出式控件：之前漏了这几条，才导致黑底黑字 ---- */

/* 下拉列表。QComboBox 展开时弹的是独立的顶层列表窗口，
   不写这条就会跟随系统调色板 —— 系统深色时字就看不见了。 */
QComboBox QAbstractItemView, QListView {{
    background: {p.bg_panel};
    color: {p.fg_normal};
    border: 1px solid {p.border_strong};
    outline: 0;
    padding: 2px;
    selection-background-color: {p.bg_select};
    selection-color: {p.fg_normal};
}}
QListView::item {{ padding: 4px 6px; border-radius: 3px; }}
QListView::item:hover {{ background: {p.bg_hover}; }}

/* 右键菜单（包括托盘图标的菜单） */
QMenu {{
    background: {p.bg_panel};
    color: {p.fg_normal};
    border: 1px solid {p.border_strong};
    border-radius: 5px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 26px 6px 18px;
    border-radius: 3px;
    background: transparent;
}}
QMenu::item:selected {{ background: {p.bg_select}; color: {p.fg_normal}; }}
QMenu::item:disabled {{ color: {p.fg_disabled}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}

/* ---- 按钮 ---- */
QPushButton {{
    background: {p.bg_panel};
    color: {p.fg_normal};
    border: 1px solid {p.border};
    border-radius: 4px;
    padding: 5px 14px;
    min-height: 18px;
}}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:pressed {{ background: {p.bg_hover}; }}
QPushButton:disabled {{ color: {p.fg_disabled}; border-color: {p.border}; }}
QPushButton[accent="true"] {{
    background: {p.accent};
    border-color: {p.accent};
    color: {p.fg_on_accent};
}}
QPushButton[accent="true"]:hover {{ background: {p.accent_hover}; }}
QPushButton[accent="true"]:pressed {{ background: {p.accent_pressed}; }}
/* 禁用的强调按钮必须变灰 —— [accent="true"] 规则定义在 :disabled 之后、
   特异性又相同，会把禁用色盖回蓝色（「开始导入」没选文件时看着像能点）。
   这条同时挂属性和伪类，特异性更高，稳赢。 */
QPushButton[accent="true"]:disabled {{
    background: {p.bg_group};
    border-color: {p.border};
    color: {p.fg_disabled};
}}
QPushButton[danger="true"] {{ color: {p.fg_out}; }}

/* 看起来像链接的按钮（BOM 右下角的可点击总价）。
   底色和边框去掉，只留文字；具体颜色由代码按状态从 theme.pal() 取，见 bom_dialog。 */
QPushButton[link="true"] {{
    background: transparent;
    border: none;
    padding: 2px 6px;
    text-align: right;
}}
QPushButton[link="true"]:hover {{ text-decoration: underline; }}
QPushButton[link="true"]:pressed {{ background: {p.bg_hover}; }}

/* ---- 复选框：不覆盖指示器，交给 Fusion + 调色板画，
        这样勾是对勾而不是一个实心色块 ---- */
QCheckBox {{ color: {p.fg_normal}; spacing: 6px; background: transparent; }}
QCheckBox:disabled {{ color: {p.fg_disabled}; }}

/* ---- 表格 ---- */
QTableView, QTableWidget {{
    background: {p.bg_panel};
    alternate-background-color: {p.bg_group};
    color: {p.fg_normal};
    border: 1px solid {p.border};
    border-radius: 4px;
    gridline-color: {p.border};
    selection-background-color: {p.bg_select};
    selection-color: {p.fg_normal};
}}
QTableView::item, QTableWidget::item {{ padding: 3px 4px; }}
QHeaderView {{ background: {p.bg_header}; border: none; }}
QHeaderView::section {{
    background: {p.bg_header};
    color: {p.fg_muted};
    border: none;
    border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
    padding: 5px 6px;
    font-weight: 500;
}}
QTableCornerButton::section {{ background: {p.bg_header}; border: none; }}

/* ---- 树 ---- */
QTreeWidget, QTreeView {{
    background: {p.bg_panel};
    color: {p.fg_normal};
    border: 1px solid {p.border};
    border-radius: 4px;
    outline: none;
}}
QTreeWidget::item, QTreeView::item {{ padding: 3px 2px; }}
QTreeWidget::item:selected, QTreeView::item:selected {{
    background: {p.bg_select};
    color: {p.fg_normal};
}}
QTreeWidget::item:hover, QTreeView::item:hover {{ background: {p.bg_hover}; }}

/* ---- 页签（BOM 操作的 比对 / 导出 / 导入）---- */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 4px;
    background: {p.bg_window};
    top: -1px;
}}
QTabBar::tab {{
    background: {p.bg_group};
    color: {p.fg_muted};
    border: 1px solid {p.border};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    padding: 6px 18px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {p.bg_window};
    color: {p.fg_normal};
}}
QTabBar::tab:hover:!selected {{
    background: {p.bg_hover};
    color: {p.fg_normal};
}}

/* ---- 分组框 ---- */
QGroupBox {{
    background: {p.bg_group};
    border: 1px solid {p.border};
    border-radius: 5px;
    margin-top: 10px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {p.fg_muted};
}}

/* ---- 其它 ---- */
QSplitter::handle {{ background: {p.bg_window}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
QSplitter::handle:hover {{ background: {p.bg_select}; }}

QStatusBar {{ background: {p.bg_group}; color: {p.fg_muted}; }}
QStatusBar::item {{ border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.scroll}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {p.scroll_hover}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {p.scroll}; border-radius: 5px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {p.scroll_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {p.tooltip_bg};
    color: {p.tooltip_fg};
    border: 1px solid {p.tooltip_border};
    padding: 3px;
}}

/* 快速查询浮窗：无边框窗口，自己去掉了边框和圆角 */
QWidget#quickWindow {{ background: {p.bg_panel}; }}
QLineEdit#quickInput {{
    border: none; border-radius: 0;
    padding: 0 14px; font-size: 15px;
    background: {p.bg_panel}; color: {p.fg_normal};
}}
QTreeWidget#quickResults {{
    border: none; border-radius: 0;
    background: {p.bg_panel}; color: {p.fg_normal};
}}
QWidget#quickBottom {{ background: {p.bg_panel}; }}
QLabel#quickHint, QLabel#quickCount {{ color: {p.fg_muted}; }}

QMessageBox {{ background: {p.bg_window}; }}
QMessageBox QLabel {{ color: {p.fg_normal}; }}

QLabel[hint="true"] {{ color: {p.fg_muted}; }}
QLabel[title="true"] {{ font-weight: 500; }}
QLabel[state="out"] {{ color: {p.fg_out}; font-weight: 500; }}
QLabel[state="low"] {{ color: {p.fg_low}; font-weight: 500; }}
"""


# ===========================================================================
#  应用
# ===========================================================================

def apply(app, mode: Mode = "auto") -> str:
    """把主题应用到 QApplication，返回实际生效的 "light" / "dark"。

    **必须用 Fusion 样式**：Windows 的原生样式（windowsvista）不完全跟随
    QPalette，深色下复选框之类的控件仍会画成系统配色，跟周围对不上。
    Fusion 是"给什么调色板就画什么颜色"，两套主题都能保持一致。
    """
    global _current

    resolved = resolve_mode(mode)
    _current = DARK if resolved == "dark" else LIGHT

    app.setStyle("Fusion")
    app.setPalette(build_palette(_current))
    app.setStyleSheet(build_qss(_current))
    return resolved
