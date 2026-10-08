"""系统托盘图标。

图标用 **`ico/EDMS.ico`**（用户给的），托盘、窗口标题栏、任务栏共用同一份。
以前是代码画一个芯片，结果 exe 换了图标、标题栏还是默认的 —— 漏就漏在
没有调 `QApplication.setWindowIcon`，托盘一个控件改不了全局。

托盘是"常驻但不碍事"的关键：主窗口关掉之后进程还在，
热键随时能唤起浮窗，但任务栏上不留东西。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app import config
from app.paths import resolve_resource

_ICON_CACHE: QIcon | None = None


def app_icon() -> QIcon:
    """程序图标。托盘、主窗口、所有对话框都用这一份。

    图标是**只读资源**，spec 里打进包（源码运行直接找项目根的 ico/）。
    万一资源丢了就退回代码画的芯片 —— 缺图标也别变成一个空白占位。
    """
    global _ICON_CACHE
    if _ICON_CACHE is None:
        path = resolve_resource("ico", "EDMS.ico")
        icon = QIcon(str(path)) if path.exists() else QIcon()
        # 图标文件在但**解不了**也会拿到空 QIcon —— 打包时要是把
        # imageformats/qico.dll 剔了就会这样，托盘和标题栏全空白。
        # 所以空了就退回代码画的芯片，别让用户看到一片光秃秃。
        _ICON_CACHE = icon if not icon.isNull() else make_tray_icon()
    return _ICON_CACHE


def make_tray_icon(size: int = 64) -> QIcon:
    """（备手）代码画一个芯片形状的图标。多尺寸一起塞进去，托盘和高分屏都清晰。"""
    icon = QIcon()
    for s in (16, 24, 32, 48, 64):
        pix = QPixmap(s, s)
        pix.fill(Qt.transparent)

        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 芯片本体占中间 56%，四周留出引脚空间
        margin = s * 0.22
        body = QRectF(margin, margin, s - 2 * margin, s - 2 * margin)

        p.setPen(QPen(QColor("#1E5B96"), max(1.0, s * 0.045)))
        p.setBrush(QColor("#3B8BE4"))
        p.drawRoundedRect(body, s * 0.10, s * 0.10)

        # 两侧引脚
        p.setPen(QPen(QColor("#1E5B96"), max(1.0, s * 0.055)))
        pins = 3
        for i in range(pins):
            frac = (i + 1) / (pins + 1)
            y = body.top() + body.height() * frac
            p.drawLine(int(0 + s * 0.04), int(y), int(body.left()), int(y))
            p.drawLine(int(body.right()), int(y), int(s - s * 0.04), int(y))

        # 中间一个亮点，小尺寸下也能看出是个"元件"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#D6E8F9"))
        dot = s * 0.14
        p.drawEllipse(
            body.center().x() - dot / 2, body.center().y() - dot / 2, dot, dot
        )
        p.end()

        icon.addPixmap(pix)

    return icon


class TrayIcon(QSystemTrayIcon):
    """托盘图标 + 右键菜单。所有动作都通过信号抛给上层，自己不做业务。"""

    show_main_requested = Signal()
    quick_search_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, hotkey_label: str, parent=None) -> None:
        super().__init__(app_icon(), parent)
        self.setToolTip("元器件管理")

        menu = QMenu()

        self.act_quick = QAction(f"快速查询（{hotkey_label}）", menu)
        self.act_quick.triggered.connect(self.quick_search_requested.emit)
        menu.addAction(self.act_quick)

        self.act_main = QAction("打开主窗口", menu)
        self.act_main.triggered.connect(self.show_main_requested.emit)
        menu.addAction(self.act_main)

        self.act_settings = QAction("设置…", menu)
        self.act_settings.triggered.connect(self.settings_requested.emit)
        menu.addAction(self.act_settings)

        menu.addSeparator()

        self.act_quit = QAction("退出", menu)
        self.act_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(self.act_quit)

        self.setContextMenu(menu)

        # 左键单击 = 唤起快速查询，符合肌肉记忆
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.quick_search_requested.emit()
        elif reason == QSystemTrayIcon.DoubleClick:
            self.show_main_requested.emit()

    def set_hotkey_label(self, label: str) -> None:
        self.act_quick.setText(f"快速查询（{label}）")

    def notify(self, title: str, message: str) -> None:
        # 气泡开关在设置面板里（settings.json 的 show_tray_notifications）。
        # 以前这个配置项定义了却没人读，是半成品；现在接通。
        if not config.get("show_tray_notifications"):
            return
        self.showMessage(title, message, self.icon(), 3000)
