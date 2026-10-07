"""首次运行时写入的默认分类、参数模板与存放位置。

只在对应表为空时执行，绝不覆盖用户已有数据。之后用户可以在界面里随便改名增删。

参数模板的默认值刻意贴近桌面电子的习惯：
    电阻的阻值以 Ω 计、电容的容值以 nF 计。
    这样 10kΩ 输 10000、100nF 输 100，都是整数，不会出现 1e-7 这种反人类的存法。
"""

from __future__ import annotations

from app.storage.db import Database

# (分类名, [(参数名, 单位, 类型)])
DEFAULT_CATEGORIES: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("电阻", [
        ("阻值", "Ω", "num"),
        ("精度", "%", "num"),
        ("功率", "W", "num"),
        ("耐压", "V", "num"),
    ]),
    ("电容", [
        ("容值", "nF", "num"),
        ("耐压", "V", "num"),
        ("精度", "%", "num"),
        ("介质", "", "text"),
    ]),
    ("电感", [
        ("感值", "uH", "num"),
        ("额定电流", "A", "num"),
        ("精度", "%", "num"),
    ]),
    ("二极管", [
        ("类型", "", "text"),
        ("耐压", "V", "num"),
        ("额定电流", "A", "num"),
    ]),
    ("三极管/MOS", [
        ("类型", "", "text"),
        ("耐压", "V", "num"),
        ("额定电流", "A", "num"),
    ]),
    ("芯片/IC", [
        ("功能", "", "text"),
        ("工作电压", "V", "num"),
        ("温度等级", "", "text"),
    ]),
    ("连接器", [
        ("针数", "P", "num"),
        ("间距", "mm", "num"),
    ]),
    ("晶振", [
        ("频率", "MHz", "num"),
        ("精度", "ppm", "num"),
    ]),
    ("传感器", [
        ("类型", "", "text"),
        ("工作电压", "V", "num"),
    ]),
    ("光电器件", [
        ("颜色", "", "text"),
        ("正向电压", "V", "num"),
    ]),
    ("按键/开关", [
        ("类型", "", "text"),
        ("行程", "mm", "num"),
    ]),
    ("模块", [
        ("功能", "", "text"),
        ("工作电压", "V", "num"),
    ]),
    ("其他", []),
]

# 位置先给一套够用的骨架；code 就是贴在抽屉上的短码，将来做标签打印直接用它。
DEFAULT_LOCATIONS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("元件柜A", "A", [
        ("A1-电阻", "A1"),
        ("A2-电容", "A2"),
        ("A3-杂项", "A3"),
    ]),
    ("元件柜B", "B", [
        ("B1-连接器", "B1"),
        ("B2-芯片", "B2"),
        ("B3-模块", "B3"),
    ]),
    ("桌面收纳", "C", []),
    ("待整理", "Z", []),
]


def seed_if_empty(db: Database) -> bool:
    """库为空时写入默认数据。返回是否实际写入了。"""
    row = db.query_one("SELECT COUNT(*) AS n FROM category")
    if row and row["n"] > 0:
        return False

    with db.transaction() as conn:
        for ci, (cat_name, templates) in enumerate(DEFAULT_CATEGORIES):
            cur = conn.execute(
                "INSERT INTO category(name, parent_id, sort_order) VALUES (?, NULL, ?)",
                (cat_name, ci * 10),
            )
            cat_id = cur.lastrowid
            for ti, (t_name, unit, dtype) in enumerate(templates):
                conn.execute(
                    "INSERT INTO param_template(category_id, name, unit, data_type, sort_order) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (cat_id, t_name, unit, dtype, ti * 10),
                )

        for li, (loc_name, code, children) in enumerate(DEFAULT_LOCATIONS):
            cur = conn.execute(
                "INSERT INTO location(name, code, parent_id, sort_order) VALUES (?, ?, NULL, ?)",
                (loc_name, code, li * 10),
            )
            parent_id = cur.lastrowid
            for si, (child_name, child_code) in enumerate(children):
                conn.execute(
                    "INSERT INTO location(name, code, parent_id, sort_order) VALUES (?, ?, ?, ?)",
                    (child_name, child_code, parent_id, si * 10),
                )
    return True
