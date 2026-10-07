"""程序入口。

启动路径刻意做成两段：

  第一段（必做）  建托盘 + 注册全局热键。这一步只加载 QtCore/QtGui/QtWidgets 的
                  托盘相关部分，不构造主窗口，因此很快。
  第二段（懒加载）主窗口对象**延迟创建**。用户第一次点「打开主窗口」或
                  在浮窗里按回车时才构造它。

这样"常驻"占用的内存里不含主窗口那一整棵控件树。
对"启动快 + 常驻内存小"这两个硬约束来说，这是最有效的一招。

    python -m app.main            正常启动（只驻留托盘）
    python -m app.main --show     启动并直接打开主窗口
    python -m app.main --check    只构造全部界面对象然后退出（用于自动化验证）
"""

from __future__ import annotations

import logging
import sys
import time

from PySide6.QtCore import QSharedMemory, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from app import config
from app.log_setup import excepthook, setup_logging
from app.domain import Services
from app.ui import theme
from app.ui.hotkey import HotkeyFilter
from app.ui.quick_search import QuickSearchWindow
from app.ui.tray import TrayIcon, app_icon

# 单实例锁必须挂在模块级，否则函数返回后它会被回收、锁就失效了
_SINGLE_INSTANCE: QSharedMemory | None = None

# 主热键被占用时的降级候选（顺序即优先级——越靠前越顺手）。
# 选的都是 Ctrl+Alt 系里较少被常用软件抢占的字母键。
FALLBACK_HOTKEYS = [
    "ctrl+alt+e",
    "ctrl+alt+q",
    "ctrl+alt+d",
    "ctrl+alt+w",
    "ctrl+alt+j",
]


class Controller:
    """把各个部件串起来。持有引用，防止被 GC 回收。"""

    def __init__(self, app: QApplication, services: Services) -> None:
        self.app = app
        self.svc = services

        self.settings = config.load()
        self.hotkey_spec: str = self.settings["hotkey"]

        # ---- 托盘 ----
        self.tray = TrayIcon(self.hotkey_label())
        self.tray.quick_search_requested.connect(self.toggle_quick)
        self.tray.show_main_requested.connect(self.show_main)
        self.tray.quit_requested.connect(self.quit)
        self.tray.show()

        # ---- 全局热键 ----
        self.hotkey = HotkeyFilter(self.toggle_quick)
        self.app.installNativeEventFilter(self.hotkey)

        if not self.hotkey.register(self.hotkey_spec):
            first_error = self.hotkey.error
            # 配置的热键很可能被别的程序占了——这在 Windows 上太常见了。
            # 按候选表依次试，先用一个能用的保证功能不断，再明确告诉用户
            # 实际生效的是哪个、以及怎么固定下来。
            for fallback in FALLBACK_HOTKEYS:
                if fallback == self.hotkey_spec:
                    continue
                if self.hotkey.register(fallback):
                    self.hotkey_spec = fallback
                    self.tray.set_hotkey_label(self.hotkey_label())
                    self.tray.notify(
                        "已自动换用其它热键",
                        f"{first_error}。\n\n已改用 {self.hotkey_label()}。\n"
                        f"想固定下来，就改 data/settings.json 里的 hotkey。",
                    )
                    break
            else:
                self.tray.notify(
                    "热键注册失败",
                    f"{first_error}。\n\n仍可点击托盘图标唤起快速查询。",
                )

        # ---- 主窗口与浮窗：这里**都不创建**，等真正用到再说 ----
        self._main_window = None
        self._quick = None

    # ==================================================================
    #  惰性构造快速查询浮窗
    # ==================================================================

    def _ensure_quick(self) -> QuickSearchWindow:
        """第一次按热键时才构造浮窗。

        实测：构造它（哪怕不显示）会让常驻内存从 ~90MB 涨到 ~118MB——
        因为要把一整棵 QtWidgets 控件树建起来。既然如此，就只在真正需要时付这笔钱。
        常驻状态下用户可能一整天不查一次，那就一整天不付。
        """
        if self._quick is None:
            self._quick = QuickSearchWindow(self.svc)
            self._quick.open_part_requested.connect(self.open_part_in_main)
        return self._quick

    def toggle_quick(self) -> None:
        self._ensure_quick().toggle()

    # ==================================================================
    #  惰性构造主窗口
    # ==================================================================

    def _ensure_main(self):
        if self._main_window is None:
            # 延迟 import：避免在没有打开主窗口的情况下也去解析这一大坨模块
            from app.ui.main_window import MainWindow

            self._main_window = MainWindow(self.svc)
            self._main_window.hidden_to_tray.connect(
                lambda _msg: self.tray.notify(
                    "仍在后台运行", f"按 {self.hotkey_label()} 可随时快速查询。"
                )
            )
            self._main_window.status_changed.connect(
                lambda text: self.tray.setToolTip(f"元器件管理　·　{text}")
            )
        return self._main_window

    def show_main(self) -> None:
        win = self._ensure_main()
        win.showNormal()
        win.raise_()
        win.activateWindow()
        win.focus_search()

    def open_part_in_main(self, part_id: int) -> None:
        """从浮窗回车进入：打开主窗口并把该器件选中。"""
        win = self._ensure_main()
        win.showNormal()
        win.raise_()
        win.activateWindow()

        win.reload_all()
        for row in range(win.model.rowCount()):
            item = win.model.row_at(row)
            if item is not None and item.id == part_id:
                win.table.selectRow(row)
                win.table.scrollTo(win.model.index(row, 0))
                break
        win._show_detail(part_id)

    # ==================================================================
    #  退出
    # ==================================================================

    def hotkey_label(self) -> str:
        return self.hotkey_spec.replace("+", " + ").upper()

    def quit(self) -> None:
        try:
            self.hotkey.unregister()
        except Exception:  # noqa: BLE001
            pass

        if self._main_window is not None:
            self._main_window._force_close = True
            self._main_window.close()

        self.tray.hide()
        self.svc.close()
        self.app.quit()


def _acquire_single_instance(app: QApplication) -> bool:
    global _SINGLE_INSTANCE
    _SINGLE_INSTANCE = QSharedMemory("ElectronicDeviceManagementSystem.single-instance")
    return bool(_SINGLE_INSTANCE.create(1))


def ms(start: float, end: float) -> float:
    """把 perf_counter 的秒差换算成毫秒。"""
    return (end - start) * 1000.0


def _out(message: str) -> None:
    """往控制台打印一行；没有控制台时安静跳过。

    打包成 --windowed 的 exe 之后进程不分配控制台，sys.stdout 会是 None，
    这时直接 print 会抛 AttributeError 把程序打挂。所以统一走这里。
    """
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(message)
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    check_mode = "--check" in argv

    # 日志必须放在最前面：启动本身、数据库打开失败这些都得有据可查。
    # 在 Services.bootstrap() 之前 —— 晚了的话库一坏连一条日志都没有。
    setup_logging()
    sys.excepthook = excepthook      # 没人接的异常进 error 文件，留下尸检报告
    log = logging.getLogger("edms.startup")
    t0 = time.perf_counter()

    app = QApplication(argv)
    app.setApplicationName("EDMS")
    app.setOrganizationName("self")
    # 全局图标：托盘、窗口标题栏、任务栏、所有对话框都用这一份。
    # 之前只给托盘设了，exe 换了图标标题栏还是默认的，就是漏了这一句。
    app.setWindowIcon(app_icon())
    # 关键：关掉最后一个窗口**不**退出程序。托盘要活着。
    app.setQuitOnLastWindowClosed(False)

    # 主题：默认跟随系统深浅色，可用 settings.json 里的 theme 强制 light/dark。
    # 必须在构造任何界面对象之前应用，否则控件会先按默认调色板画一遍。
    theme_mode = config.get("theme")
    resolved = theme.apply(app, theme_mode)
    if (theme_mode or "auto").lower() == "auto":
        # 用户在系统里切换深浅色时实时跟随
        hints = app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda _=None: theme.apply(app, "auto"))

    if not check_mode and not _acquire_single_instance(app):
        log.warning("单实例拦截：已有一个 EDMS 在运行")
        QMessageBox.information(
            None, "已经在运行",
            "元器件管理已经在运行了。\n\n看看右下角托盘里的图标。",
        )
        return 0

    services = Services.bootstrap()
    t1 = time.perf_counter()
    log.info("启动完成（%.0f ms），check=%s", (t1 - t0) * 1000, check_mode)

    controller = Controller(app, services)
    t2 = time.perf_counter()

    if check_mode:
        # 把主窗口也构造出来，验证整棵界面树没问题，然后立刻退出
        controller._ensure_main()
        _out(f"[check] 数据库就绪      {ms(t0, t1):6.0f} ms")
        _out(f"[check] 托盘+热键就绪    {ms(t1, t2):6.0f} ms")
        _out(f"[check] 主窗口构造完成   {ms(t2, time.perf_counter()):6.0f} ms")
        _out(f"[check] 合计            {ms(t0, time.perf_counter()):6.0f} ms")
        part_count = services.db.query_one("SELECT COUNT(*) AS n FROM part")["n"]
        _out(f"[check] 器件数          {part_count}")
        _out(f"[check] 数据目录        {services.db.db_path.parent}")
        hotkey_state = "成功" if controller.hotkey.enabled else f"失败：{controller.hotkey.error}"
        _out(f"[check] 热键            {controller.hotkey_label()} (注册{hotkey_state})")
        controller.quit()
        return 0

    _out(f"就绪（{ms(t0, t2):.0f} ms）：托盘已启动，"
         f"按 {controller.hotkey_label()} 快速查询。")

    # --show：启动即打开主窗口。给桌面快捷方式用，省得每次还要点托盘。
    if "--show" in argv:
        controller.show_main()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
