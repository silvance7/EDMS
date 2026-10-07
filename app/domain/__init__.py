"""领域层：业务模型与服务。

**本包不 import 任何 PySide6 / Qt 模块。**
这条纪律的价值在于：核心逻辑可以脱离界面直接跑测试、写脚本批量导入，
将来就算换掉整个界面（Qt 之外的选择），业务代码一行都不用动。
"""

from app.storage.db import Database
from app.domain.part_service import PartService
from app.domain.search_service import SearchService
from app.domain.stock_service import InsufficientStockError, StockService
from app.domain.tree_service import TreeService

from app.storage.migrations import run_migrations
from app.domain.seed import seed_if_empty

__all__ = [
    "Services",
    "InsufficientStockError",
]


class Services:
    """服务门面。界面层只需要持有这一个对象，不用挨个 import 各 service。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.tree = TreeService(db)
        self.parts = PartService(db, self.tree)
        self.stock = StockService(db)
        self.search = SearchService(db, self.tree)

    @classmethod
    def bootstrap(cls, db_path: str | None = None) -> "Services":
        """一步到位：建库 → 迁移 → 首次运行灌默认分类和位置。"""
        db = Database(db_path)
        run_migrations(db)
        seed_if_empty(db)
        return cls(db)

    def close(self) -> None:
        self.db.close()
