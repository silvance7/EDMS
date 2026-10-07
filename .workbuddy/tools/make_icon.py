"""生成程序图标 build/icon.ico。

图标是**代码画出来的**，跟托盘图标共用同一个绘制函数，
所以不会出现"exe 图标和托盘图标长得不一样"这种别扭事，也不用往仓库里塞二进制。

ICO 里直接嵌 PNG（Vista 之后 Windows 完全支持）：
比自己手写 BMP + alpha 掩码简单得多，256x256 那一档也不会膨胀成几 MB。

    .venv/Scripts/python.exe .workbuddy/tools/make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice      # noqa: E402
from PySide6.QtGui import QImage                                # noqa: E402
from PySide6.QtWidgets import QApplication                      # noqa: E402

from app.ui.tray import make_tray_icon                          # noqa: E402

# Windows 会按需挑最合适的一档；256 是任务栏大图标和资源管理器超大图标用的
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

OUT_PATH = Path(__file__).resolve().parents[2] / "build" / "icon.ico"


def png_bytes(icon, size: int) -> bytes:
    """把图标渲染成指定尺寸的 PNG 字节流。"""
    pixmap = icon.pixmap(size, size)
    # 统一成带 alpha 的 32 位格式，否则小尺寸下边缘会出现黑边
    image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)

    # QByteArray 必须用一个具名变量显式持有引用。
    # 写成 QBuffer(QByteArray()) 的话，那个临时对象在语句结束后就被回收，
    # QBuffer 随即指向已释放的内存 —— 表现是**直接段错误**，连异常都没有。
    storage = QByteArray()
    buffer = QBuffer(storage)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(buffer.data())


def build_ico(png_images: list[tuple[int, bytes]], out_path: Path) -> None:
    """组装 ICO 文件。

    结构：
        ICONDIR        6 字节   保留位 + 类型(1=图标) + 图像数量
        ICONDIRENTRY   16 字节  每个图像一条，写明尺寸、大小、数据偏移
        图像数据        紧跟其后，这里直接是完整的 PNG 文件
    """
    count = len(png_images)
    header = struct.pack("<HHH", 0, 1, count)

    offset = len(header) + 16 * count
    entries = bytearray()
    payload = bytearray()

    for size, data in png_images:
        # 宽高各占 1 字节，0 表示 256
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0,          # 调色板颜色数，32 位图固定写 0
            0,          # 保留
            1,          # 颜色平面数
            32,         # 每像素位数
            len(data),
            offset,
        )
        payload += data
        offset += len(data)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(header + bytes(entries) + bytes(payload))


def main() -> None:
    # QIcon 需要 QApplication 存在才能拿 pixmap。
    # 注意不要显式 del 掉它——QApplication 析构后再销毁 QIcon/QPixmap 同样会段错误，
    # 交给解释器退出时自然回收即可。
    QApplication(sys.argv)
    icon = make_tray_icon()

    images = [(size, png_bytes(icon, size)) for size in SIZES]
    build_ico(images, OUT_PATH)

    total = OUT_PATH.stat().st_size
    print(f"已生成 {OUT_PATH}（{total / 1024:.1f} KB，{len(images)} 档尺寸）")
    for size, data in images:
        print(f"    {size:>3}x{size:<3}  {len(data) / 1024:6.1f} KB")


if __name__ == "__main__":
    main()
