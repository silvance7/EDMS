"""路径解析：让同一份代码在"源码运行"和"打包后运行"两种情况下都能找对地方。

打包后（PyInstaller）有两条完全不同的路，混在一起会出人命：

  只读的程序资源  在 sys._MEIPASS 里（onedir 下就是 _internal/）。
                  代码、Qt 的 dll、schema.sql 都在这儿。
  可写的数据      **绝对不能**放那儿 —— onefile 模式下 _MEIPASS 是个临时目录，
                  进程退出就被清掉，用户辛苦录的库存会凭空消失。
                  哪怕是 onedir，数据放在 _internal 里也会在重新打包时被覆盖。

所以数据目录固定取 **exe 所在目录下的 data/**。这样打出来是个绿色便携版：
整个文件夹拷到 U 盘、拷到另一台电脑都能直接用，数据跟着走。
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出来的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序根目录。

    打包后 = exe 所在目录；源码运行 = 项目根目录。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_dir() -> Path:
    """只读资源所在目录（Qt 的 dll、schema.sql 等）。

    打包后是 sys._MEIPASS；源码运行时就是项目根。
    """
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else app_root()


def data_dir(create: bool = True) -> Path:
    """可写的数据目录：exe 旁边的 data/。"""
    path = app_root() / "data"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir(create: bool = True) -> Path:
    """可写的日志目录：exe 旁边的 log/，与 data/ 同级。

    和 data/ 同一条铁律 —— 打包后绝不能落进 _MEIPASS（临时目录，
    进程退出就被清掉）。日志是运行期产物，只认 app_root()。
    """
    path = app_root() / "log"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_resource(*parts: str) -> Path:
    """定位只读资源，例如 resolve_resource("storage", "schema.sql")。

    依次尝试：
        1. 源码运行   —— app/<parts>
        2. 便携版      —— exe 旁边的 <parts>
        3. 打包后      —— _MEIPASS/app/<parts>（spec 里把资源放在 app/ 下镜像源码布局）
        4. 打包后扁平  —— _MEIPASS/<parts>

    返回第一个真实存在的；都不存在则返回第一个候选，让调用方自己的
    报错信息里能看到一个合理的路径，而不是一句莫名其妙的 None。
    """
    candidates = [
        Path(__file__).resolve().parents[1].joinpath(*parts),
        Path(sys.executable).resolve().parent.joinpath(*parts),
        bundle_dir().joinpath("app", *parts),
        bundle_dir().joinpath(*parts),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]
