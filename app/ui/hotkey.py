"""Windows 全局热键。

为什么不用 `keyboard` / `pynput` 这类第三方库：
它们靠安装全局键盘钩子（SetWindowsHookEx）实现，部分安全软件会拦，
而且多一个依赖。Windows 自带的 RegisterHotKey 是官方机制：
内核把组合键直接投递给你的线程，不需要钩子，不需要管理员权限。

两个必须注意的点：
  1. hwnd 传 NULL 时，WM_HOTKEY 是投递到**线程消息队列**的。
     Qt 的事件分发器在取消息时会调用 QAbstractNativeEventFilter，
     所以这里用原生事件过滤器接住它，不需要自己建隐藏窗口。
  2. 消息结构体要用 ctypes.wintypes.MSG 解析，不能按指针直接读。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter

# --- Win32 常量 ---
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

_USER32 = ctypes.windll.user32

_MOD_MAP = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """把 "ctrl+alt+p" 解析成 (修饰键掩码, 虚拟键码)。

    虚拟键码规则：
        单字符 A-Z / 0-9  -> 直接用大写字母的 ASCII
        F1-F24           -> 0x70 + (n-1)
        其它支持的名字    -> 见 _NAMED_KEYS
    """
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("热键不能为空")

    modifiers = 0
    key_token: str | None = None

    for part in parts:
        if part in _MOD_MAP:
            modifiers |= _MOD_MAP[part]
            continue
        if key_token is not None:
            raise ValueError(f"热键里只能有一个主键：{spec}")
        key_token = part

    if key_token is None:
        raise ValueError(f"热键缺少主键：{spec}")

    if len(key_token) == 1 and (key_token.isalnum()):
        vk = ord(key_token.upper())
    elif key_token.startswith("f") and key_token[1:].isdigit():
        n = int(key_token[1:])
        if not 1 <= n <= 24:
            raise ValueError(f"功能键只支持 F1-F24：{key_token}")
        vk = 0x70 + (n - 1)
    elif key_token in _NAMED_KEYS:
        vk = _NAMED_KEYS[key_token]
    else:
        raise ValueError(f"无法识别的按键：{key_token}")

    # MOD_NOREPEAT：按住不放时只触发一次，避免浮窗疯狂开关
    return modifiers | MOD_NOREPEAT, vk


class HotkeyFilter(QAbstractNativeEventFilter):
    """接住 WM_HOTKEY 并回调。注册失败时 enabled=False，程序照常运行。"""

    def __init__(self, on_trigger: Callable[[], None]) -> None:
        super().__init__()
        self._on_trigger = on_trigger
        self._hotkey_id = 0
        self.enabled = False
        self.error = ""

    def register(self, spec: str, hotkey_id: int = 1) -> bool:
        try:
            modifiers, vk = parse_hotkey(spec)
        except ValueError as exc:
            self.error = str(exc)
            return False

        ok = bool(_USER32.RegisterHotKey(None, hotkey_id, modifiers, vk))
        if not ok:
            # 最常见的失败原因就是这个组合键已经被别的程序占用了
            self.error = f"热键 {spec.upper()} 注册失败，可能已被其它程序占用"
            return False

        self._hotkey_id = hotkey_id
        self.enabled = True
        return True

    def unregister(self) -> None:
        if self.enabled and self._hotkey_id:
            _USER32.UnregisterHotKey(None, self._hotkey_id)
        self.enabled = False

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if self.enabled and event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self._hotkey_id:
                self._on_trigger()
        # 返回 (False, 0) 表示不吞掉消息，继续交给 Qt 正常处理
        return False, 0


_NAMED_KEYS: dict[str, int] = {
    "space": 0x20,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}
