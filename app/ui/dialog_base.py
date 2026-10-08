"""对话框公共基类。

**回车守卫（EnterSafeDialog）** —— 这个项目里最坑的一个 Qt 行为：

在 QDialog 里按回车，Qt 会把事件交给「默认按钮」（QDialogButtonBox 的
第一个按钮会自动成为默认按钮）。入库面板正是因此出过事故：搜不到器件时
列表里唯一一行是「＋新建」，回车确认后**同一个按键事件**又触发了「入库」
按钮 —— 光标还停在名称框里，器件就已经带着默认数量 1 落库了
（无分类、无参数、而且不报错）。

试过并**证伪**的方案（别再走这条路）：
    button.setAutoDefault(False) + button.setDefault(False)
实测（Qt 6.9）拦不住：对话框 show 之后默认按钮会被重新指认，
回车照样触发「入库」。

真正有效的方案：在**对话框层**覆写 keyPressEvent —— 按键是回车/小键盘
回车、且当前焦点不在按钮上时，吞掉这个事件。两个要点：
  · 焦点在按钮上（用户明摆着就是想按它）时保持原生行为，不误吞；
  · QLineEdit.returnPressed 在事件传播**之前**就已触发 —— 搜索框里
    回车「选中第一项」这类逻辑不受影响（实测确认）。

新写的、有默认按钮的对话框一律继承本类：
    from app.ui.dialog_base import EnterSafeDialog

（BomDialog 有意不接这个守卫 —— 它唯一的按钮是「关闭」，没有会写库的
默认动作；TreeManageDialog 也保持原样。这是有意划的边界，别"顺手统一"。）
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QPushButton


class EnterSafeDialog(QDialog):
    """回车不触发默认按钮的对话框基类（根因与实验结论见模块注释）。"""

    def keyPressEvent(self, event) -> None:  # noqa: N802 - 跟随 Qt 命名
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            focus = QApplication.focusWidget()
            # 焦点不在按钮上 → 回车不提交。会落库/落盘的对话框都靠这条保命。
            if not isinstance(focus, QPushButton):
                return
        super().keyPressEvent(event)
