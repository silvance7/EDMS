"""日志初始化：log/ 目录两类文件，info 记完整时间线，error 只记报错。

必须用 stdlib 的 logging，**不能 import Qt** —— domain 层（分层铁律：不 import Qt）
也要用它记业务动作，否则入库/出库从托盘、主窗口、BOM 三个入口触发就没法统一记。

三个口径都是用户拍板的：

1. **info 文件 = 完整时间线**，错误也写在里面，方便按时间顺序复盘；
   error 文件是纯粹的报错索引。所以 info handler **不加**级别过滤。
2. **粒度只到器件级动作**（入库/出库/盘点/改价），不记搜索、开面板这类
   高频低价值事件 —— 那会淹没真正有用的记录。
3. **文件名自带日期，且日期在扩展名之前**：`info.2026-10-08.log` /
   `error.2026-10-08.log`。见下面"为什么不用 TimedRotating"。

编码必须显式 utf-8：日志里全是中文（器件名、位置名），不写 encoding
在 Windows 上会按 GBK 走，遇到生僻字直接 UnicodeEncodeError。

清理：_prune_old_logs() 每次启动兜底清一遍，按修改时间每类保留最新 10 个
（KEEP）——防时钟异常、长期运行或手工塞文件导致的堆积。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from app.paths import log_dir

KEEP = 10                       # 每类日志最多保留几个文件
_FMT = "%(asctime)s [%(levelname).1s] %(name)s: %(message)s"
_PREFIXES = ("info", "error")


def _today() -> str:
    """今天的日期串，形如 2026-10-08（文件名里用）。"""
    return date.today().strftime("%Y-%m-%d")


def daily_log_path(prefix: str, day: str | None = None,
                   directory: Path | None = None) -> Path:
    """按天的日志文件路径：`<prefix>.<YYYY-MM-DD>.log`。

    **日期在扩展名之前**是用户定的格式：文件本身就标明是哪天的，
    当前文件与历史文件长得一模一样，不用猜。
    """
    base = Path(directory) if directory is not None else log_dir()
    return base / f"{prefix}.{day or _today()}.log"


class DailyFileHandler(logging.FileHandler):
    """一天一个文件的 handler：写第一条时若已跨天，自动换到新文件。

    为什么不用 TimedRotatingFileHandler(when="midnight")：
        它的滚动产物是 `<基名>.<日期>` —— 基名 `info.log` 就把扩展名写死了，
        日期只能拼在**后面**，产物是 `info.log.2026-10-08`：日期跑到扩展名之后，
        而"今天正在写的那个"还是不带日期的 info.log，得靠猜才知道它是哪天的。
        用户要的是 `info.2026-10-08.log`，标准库给不了，只能自己按天开文件。

    跨天切换做成**惰性**：只有真的要写日志时才比对日期。没有日志就没有新记录，
    不需要挂定时器空转；托盘常驻跨天同样有效（下一条日志自动落到新文件）。
    """

    def __init__(self, prefix: str, **kwargs) -> None:
        self._prefix = prefix
        self._day = _today()
        super().__init__(str(daily_log_path(prefix, self._day)), **kwargs)

    def emit(self, record) -> None:
        today = _today()
        if today != self._day:
            self._roll_to(today)
        super().emit(record)

    def _roll_to(self, day: str) -> None:
        """关掉昨天的句柄，把目标指向今天 —— 下次 emit 由基类重新打开。"""
        self.close()
        self._day = day
        self.baseFilename = str(daily_log_path(self._prefix, day))


def _prune_old_logs(keep: int = KEEP) -> None:
    """按修改时间删最旧的日志，info / error 各自独立计数。

    只匹配本模块的产物 `info.<日期>.log` / `error.<日期>.log`，
    别的文件一概不碰（含旧格式 `info.log.2026-10-07` 这类历史遗留）。
    """
    for prefix in _PREFIXES:
        files = sorted(log_dir(create=False).glob(f"{prefix}.*.log"),
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
    _prune_old_logs()           # 装 handler 之前清，免得误删正在写的文件

    # info = 完整时间线（含错误，用户拍板），所以**不加**级别过滤
    info_h = DailyFileHandler("info", encoding="utf-8")
    info_h.setLevel(logging.INFO)
    info_h.setFormatter(formatter)
    root.addHandler(info_h)

    # error = 纯报错索引
    err_h = DailyFileHandler("error", encoding="utf-8")
    err_h.setLevel(logging.ERROR)
    err_h.setFormatter(formatter)
    root.addHandler(err_h)

    logging.getLogger("edms").info(
        "日志初始化完成，目录 %s，今天的文件 %s / %s",
        log_dir(), info_h.baseFilename, err_h.baseFilename)


def excepthook(exc_type, exc, tb) -> None:
    """给 sys.excepthook 用的全局兜底：没接住的异常全进 error 文件。

    UI 层那些 `except Exception` 是"接住了弹框"，走 log.exception；
    这里管的是**没人接**的 —— 一旦走到这，通常是要崩了，必须留尸检报告。
    """
    logging.getLogger("edms.fatal").critical(
        "未捕获异常", exc_info=(exc_type, exc, tb))