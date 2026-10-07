"""日志初始化：log/ 目录两类文件，info 记完整时间线，error 只记报错。

必须用 stdlib 的 logging，**不能 import Qt** —— domain 层（分层铁律：不 import Qt）
也要用它记业务动作，否则入库/出库从托盘、主窗口、BOM 三个入口触发就没法统一记。

两个口径都是用户拍板的：

1. **info 文件 = 完整时间线**，错误也写在里面，方便按时间顺序复盘；
   error 文件是纯粹的报错索引。所以 info handler **不加**级别过滤。
2. **粒度只到器件级动作**（入库/出库/盘点/改价），不记搜索、开面板这类
   高频低价值事件 —— 那会淹没真正有用的记录。

编码必须显式 utf-8：日志里全是中文（器件名、位置名），不写 encoding
在 Windows 上会按 GBK 走，遇到生僻字直接 UnicodeEncodeError。

清理：TimedRotatingFileHandler(when="midnight", backupCount=10) 每天午夜
滚动并自动删第 11 个以前的旧文件（标准库行为）；_prune_old_logs() 在启动时
再兜底清一遍 —— 防时钟异常或长期不滚动导致的堆积。
"""
from __future__ import annotations

import logging
import logging.handlers

from app.paths import log_dir

KEEP = 10                       # 每类日志最多保留几个文件
_FMT = "%(asctime)s [%(levelname).1s] %(name)s: %(message)s"


def _prune_old_logs(keep: int = KEEP) -> None:
    """按修改时间删最旧的日志，info / error 各自独立计数。

    TimedRotatingFileHandler 只在**跨午夜滚动时**才删超量的旧文件，
    这里兜底：程序长期不重启、或时钟异常导致文件堆积时也能清掉。
    只匹配自己的滚动产物（info.log.2026-10-06），不碰别的文件。
    """
    for prefix in ("info", "error"):
        files = sorted(log_dir(create=False).glob(f"{prefix}.log.*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass            # 被占用就留给下次启动再清


def setup_logging() -> None:
    """在 main() 最开头调一次。重复调用安全（先摘掉旧 handler 再装新的）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(_FMT)
    log_dir()                   # 目录不存在就建

    # info = 完整时间线（含错误，用户拍板），所以**不加**级别过滤
    info_h = logging.handlers.TimedRotatingFileHandler(
        log_dir() / "info.log", when="midnight", backupCount=KEEP,
        encoding="utf-8")
    info_h.setLevel(logging.INFO)
    info_h.setFormatter(formatter)
    root.addHandler(info_h)

    # error = 纯报错索引
    err_h = logging.handlers.TimedRotatingFileHandler(
        log_dir() / "error.log", when="midnight", backupCount=KEEP,
        encoding="utf-8")
    err_h.setLevel(logging.ERROR)
    err_h.setFormatter(formatter)
    root.addHandler(err_h)

    logging.getLogger("edms").info("日志初始化完成，目录 %s", log_dir())


def excepthook(exc_type, exc, tb) -> None:
    """给 sys.excepthook 用的全局兜底：没接住的异常全进 error 文件。

    UI 层那些 `except Exception` 是"接住了弹框"，走 log.exception；
    这里管的是**没人接**的 —— 一旦走到这，通常是要崩了，必须留尸检报告。
    """
    logging.getLogger("edms.fatal").critical(
        "未捕获异常", exc_info=(exc_type, exc, tb))
