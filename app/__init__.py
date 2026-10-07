"""本地电子元器件管理系统。

分层：
    app.storage  —— SQLite 连接、建表、迁移（唯一持有连接的地方）
    app.domain   —— 领域模型与业务服务（纯 Python，不 import Qt，可单独测试）
    app.ui       —— PySide6 界面（只调用 domain，不写 SQL）

依赖方向严格单向：ui -> domain -> storage。
"""

__version__ = "0.1.0"
