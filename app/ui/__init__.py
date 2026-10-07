"""界面层：PySide6 实现。只调用 app.domain 暴露的服务，不直接写 SQL。"""

from __future__ import annotations

from app.ui.theme import apply, pal


def format_money(value: float) -> str:
    """金额文本。最多 6 位小数，去掉尾零。

    **不要用 `:g`** —— 电子元器件的单价经常小到 0.0086、0.000086，
    `:g` 会写成 `8.6e-05` 这种科学计数法，表格里根本读不出来。

    0.0 会被 rstrip 掏成空串，所以最后要兜一个 "0"。
    """
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_money_total(value: float) -> str:
    """**总价**用：满 1 块就按两位小数带千分位（¥1,234.56），更符合读钱的习惯。

    单价不能用这个 —— 0.0086 会被两位小数抹成 0.01。
    不满 1 块时退回 format_money 的精度，别把小料的价格四舍五入没了。
    """
    if abs(value) >= 1.0:
        return f"{value:,.2f}"
    return format_money(value)


__all__ = ["apply", "pal", "format_money", "format_money_total"]
