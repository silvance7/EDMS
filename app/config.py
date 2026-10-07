"""本地配置：热键、窗口位置之类的小设置。

存成一个 JSON 文件放在 data/ 下，跟数据库放一起。
不引 configparser / pydantic——总共就几项配置，读一个 dict 足够了。
文件损坏或字段缺失时一律退回默认值，绝不因为配置读不出来就打不开程序。
"""

from __future__ import annotations

import json
from typing import Any

from app.paths import data_dir

# 跟数据库放一起（exe 旁边的 data/），打包后也是同一个位置——见 app/paths.py
CONFIG_PATH = data_dir() / "settings.json"

DEFAULTS: dict[str, Any] = {
    # 默认 Ctrl+Alt+E（E 取 Element / 元件）。
    # Ctrl+Alt+字母 这类组合在 Windows 上很容易被其它程序（输入法 / 显卡驱动 /
    # 截图工具）抢占，RegisterHotKey 会失败（错误码 1409）—— 所以程序内置了
    # 降级候选链（见 app/main.py 的 FALLBACK_HOTKEYS），注册失败时顺延到下一个
    # 可用组合，并用托盘气泡提示实际生效的是哪个。
    "hotkey": "ctrl+alt+e",
    # auto = 跟随系统深浅色；也可以强制 "light" / "dark"
    "theme": "auto",
    # 上次打开 BOM 的目录，下次直接定位过去
    "bom_dir": "",
    # 入库表单记住上次填的厂商 / 供应商 / 位置。
    # 拆快递时是连着录十几种料，每次重敲一遍来源很烦。
    "last_manufacturer": "",
    "last_supplier": "",
    "last_location_id": None,
    # 入库时"单价"那一栏的报价单位（1=元/颗, 100=元/百颗, 1000=元/千颗）。
    # 只决定"怎么填"，不改变库存里存的单颗价格。默认 1 = 和以前的填法一致。
    "last_price_unit": 1,
    "show_tray_notifications": True,
}


def load() -> dict[str, Any]:
    settings = dict(DEFAULTS)
    if not CONFIG_PATH.exists():
        return settings
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            settings.update({k: v for k, v in raw.items() if k in DEFAULTS})
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # 配置坏了就用默认值，不要把程序拖死
        pass
    return settings


def save(settings: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in settings.items() if k in DEFAULTS})
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get(key: str) -> Any:
    return load().get(key, DEFAULTS.get(key))


def set_value(key: str, value: Any) -> None:  # noqa: A001 - 语义上就该叫 set
    settings = load()
    settings[key] = value
    save(settings)
