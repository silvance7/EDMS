"""本地电子元器件管理系统。

分层：
    app.storage  —— SQLite 连接、建表、迁移（唯一持有连接的地方）
    app.domain   —— 领域模型与业务服务（纯 Python，不 import Qt，可单独测试）
    app.ui       —— PySide6 界面（只调用 domain，不写 SQL）

依赖方向严格单向：ui -> domain -> storage。
"""

# 版本号的**单一来源**：主窗口状态栏显示它。发布新版本时三处同步改：
# pyproject.toml 的 [project].version、本文件、README 的徽章/下载链接
# （流程见 progress.md 的「发布新版本」一节）。
__version__ = "1.1.1"
