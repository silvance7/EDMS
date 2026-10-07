"""测试数据：一批"标准器件" + 与之配套的测试 BOM。

    .venv/Scripts/python.exe .workbuddy/tools/test_data.py            # 导两个库 + 生成测试 BOM
    .venv/Scripts/python.exe .workbuddy/tools/test_data.py --dry-run  # 只看会做什么，不动库
    .venv/Scripts/python.exe .workbuddy/tools/test_data.py --clear    # 按名单删掉这批测试器件
    .venv/Scripts/python.exe .workbuddy/tools/test_data.py --db data/inventory.db

为什么把器件表和 BOM 放同一份清单里：**两边必须对得上**。
手写一份库、再手写一份 BOM，改一个封装就有一行匹配不上，
结果是"测试数据本身有 bug"，排查半天。

**刻意的设计**：

1. 名称、型号、封装都按真实器件写，覆盖电阻/电容/电感/二极管/三极管 MOS/
   芯片 IC/连接器/晶振/光电器件/按键开关 十类。
2. **有 5 个器件故意不填单价**（`price=0`，但数量 > 0）。
   这样 BOM 成本明细里会同时出现绿色总价和黄底待补价两种状态，
   方便验证"补价 → 写库 → 总价变绿"这条链路。
3. 重名器件跳过，所以**重复执行不会堆重复数据**；`--clear` 按名单精确删除。
4. 导入前自动备份数据库（`<库文件>.bak-<时间戳>`），因为这是往你**正在用的库**里写。

BOM 里的封装写成 R0603 / C0603 / L0603 这种 KiCad 风格，
就是为了顺带验证封装归一化（库里存的是 0603）。
"""

from __future__ import annotations

import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.domain import Services                      # noqa: E402
from app.domain.models import Part                   # noqa: E402

BOM_NAME = "测试BOM_标准器件清单.csv"

# 打在每个测试器件 keywords 上的标记。**`--clear` 靠它来保命** ——
# 名单里有几个器件（STM32F103C8T6、TB6612FNG…）你在导入前就已经有了，
# 只按名字删会把你的真实数据一起删掉。
# 顺带好处：主窗口搜「测试数据」就能把所有测试器件筛出来。
TEST_MARK = "测试数据"

# (名称, 分类, 型号, 厂商, 库封装, BOM封装, BOM 值, BOM 用量, 关键词, 预警阈值,
#  参数, [(位置短码, 数量, 单价, 购买日期, 供应商)])
#
# 单价填 0 又不填日期 = **故意留的空价**，数量的确有货，用来测黄色待补价。
# 单价的单位是"元/颗"（库里永远存单颗价，见 progress.md 关键决策第 10 条）。
PARTS: list[tuple] = [
    # ---------------- 电阻 ----------------
    ("10kΩ 0603 1% 电阻", "电阻", "RC0603FR-0710KL", "Yageo", "0603", "R0603",
     "10k", 24, "res 电阻 上拉", 100,
     {"阻值": 10000.0, "精度": 1.0, "功率": 0.1},
     [("A1", 500, 0.0086, "2026-03-15", "立创商城")]),

    ("4.7kΩ 0603 1% 电阻", "电阻", "RC0603FR-074K7L", "Yageo", "0603", "R0603",
     "4.7k", 8, "res 电阻", 50,
     {"阻值": 4700.0, "精度": 1.0, "功率": 0.1},
     [("A1", 300, 0.0080, "2026-03-15", "立创商城")]),

    ("1kΩ 0603 1% 电阻", "电阻", "RC0603FR-071KL", "Yageo", "0603", "R0603",
     "1k", 12, "res 电阻", 50,
     {"阻值": 1000.0, "精度": 1.0, "功率": 0.1},
     [("A1", 400, 0.0080, "2026-03-15", "立创商城")]),

    ("100Ω 0603 1% 电阻", "电阻", "RC0603FR-07100RL", "Yageo", "0603", "R0603",
     "100", 6, "res 电阻", 50,
     {"阻值": 100.0, "精度": 1.0, "功率": 0.1},
     [("A1", 220, 0.0080, "2026-03-15", "立创商城")]),

    ("330Ω 0603 1% 电阻", "电阻", "RC0603FR-07330RL", "Yageo", "0603", "R0603",
     "330", 10, "res 电阻 led限流", 50,
     {"阻值": 330.0, "精度": 1.0, "功率": 0.1},
     [("A1", 300, 0.0082, "2026-05-20", "立创商城")]),

    # ↓ 故意没填单价：数量的确有货，但库里查不到价 —— 成本明细里应当是黄底
    ("5.1kΩ 0603 1% 电阻", "电阻", "RC0603FR-075K1L", "Yageo", "0603", "R0603",
     "5.1k", 4, "res 电阻", 50,
     {"阻值": 5100.0, "精度": 1.0, "功率": 0.1},
     [("A1", 150, 0.0, "", "立创商城")]),

    # 两条批次、两个价 —— 验证均价是按数量加权的，不是简单平均
    ("10kΩ 0805 1% 电阻", "电阻", "RC0805FR-0710KL", "Yageo", "0805", "R0805",
     "10k", 8, "res 电阻", 50,
     {"阻值": 10000.0, "精度": 1.0, "功率": 0.125},
     [("A1", 900, 0.0120, "2026-03-15", "立创商城"),
      ("A1", 100, 0.0240, "2026-08-11", "淘宝")]),

    ("1MΩ 0603 1% 电阻", "电阻", "RC0603FR-071ML", "Yageo", "0603", "R0603",
     "1M", 2, "res 电阻 下拉", 20,
     {"阻值": 1000000.0, "精度": 1.0, "功率": 0.1},
     [("A1", 80, 0.0085, "2026-03-15", "立创商城")]),

    # ---------------- 电容 ----------------
    ("100nF 0603 50V X7R 电容", "电容", "CL10B104KB8NNNC", "Samsung", "0603", "C0603",
     "100nF", 40, "cap 电容 104 去耦", 200,
     {"容值": 100.0, "耐压": 50.0, "精度": 10.0, "介质": "X7R"},
     [("A2", 800, 0.0125, "2026-03-15", "立创商城")]),

    ("1uF 0603 25V X7R 电容", "电容", "CL10B105KA8NNNC", "Samsung", "0603", "C0603",
     "1uF", 18, "cap 电容 105", 100,
     {"容值": 1000.0, "耐压": 25.0, "精度": 10.0, "介质": "X7R"},
     [("A2", 400, 0.0220, "2026-05-20", "立创商城")]),

    ("10uF 0805 25V X5R 电容", "电容", "CL21A106KAYNNNE", "Samsung", "0805", "C0805",
     "10uF", 12, "cap 电容 106", 100,
     {"容值": 10000.0, "耐压": 25.0, "精度": 10.0, "介质": "X5R"},
     [("A2", 220, 0.0850, "2026-05-11", "立创商城")]),

    ("22pF 0603 50V C0G 电容", "电容", "0603CG220J500NT", "FH", "0603", "C0603",
     "22pF", 2, "cap 电容 晶振负载", 20,
     {"容值": 22.0, "耐压": 50.0, "精度": 5.0, "介质": "C0G"},
     [("A2", 60, 0.0310, "2025-11-08", "淘宝")]),

    ("100uF 1206 25V 电容", "电容", "CL31A107MQHNNNE", "Samsung", "1206", "C1206",
     "100uF", 4, "cap 电容 电解 107", 30,
     {"容值": 100000.0, "耐压": 25.0, "精度": 20.0, "介质": "X5R"},
     [("A2", 90, 0.3200, "2026-06-02", "立创商城")]),

    # ↓ 故意没填单价
    ("22uF 0805 16V X5R 电容", "电容", "CL21A226KOYNNNE", "Samsung", "0805", "C0805",
     "22uF", 6, "cap 电容 226", 50,
     {"容值": 22000.0, "耐压": 16.0, "精度": 10.0, "介质": "X5R"},
     [("A2", 130, 0.0, "", "立创商城")]),

    # ---------------- 电感 ----------------
    ("10uH 0805 电感", "电感", "CBMF1608T100K", "Taiyo", "0805", "L0805",
     "10uH", 3, "ind 电感 功率电感", 20,
     {"感值": 10.0, "额定电流": 0.5, "精度": 10.0},
     [("A3", 45, 0.1500, "2026-04-09", "立创商城")]),

    ("4.7uH 0603 电感", "电感", "MLZ1608N4R7LT", "TDK", "0603", "L0603",
     "4.7uH", 2, "ind 电感", 20,
     {"感值": 4.7, "额定电流": 0.3, "精度": 20.0},
     [("A3", 30, 0.0900, "2026-04-09", "立创商城")]),

    # ---------------- 二极管 ----------------
    ("1N4148W 开关二极管", "二极管", "1N4148W", "Vishay", "SOD-123", "D_SOD-123",
     "1N4148W", 10, "diode 二极管 开关", 50,
     {"类型": "开关二极管", "耐压": 100.0, "额定电流": 0.15},
     [("A3", 250, 0.0350, "2026-03-15", "立创商城")]),

    ("SS14 肖特基二极管", "二极管", "SS14", "MDD", "DO-214AC(SMA)", "D_SMA",
     "SS14", 4, "diode 肖特基 续流", 30,
     {"类型": "肖特基", "耐压": 40.0, "额定电流": 1.0},
     [("A3", 160, 0.0450, "2026-03-15", "立创商城")]),

    # ↓ 故意没填单价
    ("SS34 肖特基二极管", "二极管", "SS34", "MDD", "DO-214AC(SMA)", "D_SMA",
     "SS34", 4, "diode 肖特基 整流", 30,
     {"类型": "肖特基", "耐压": 40.0, "额定电流": 3.0},
     [("A3", 120, 0.0, "", "淘宝")]),

    ("1N4007 整流二极管", "二极管", "1N4007", "MDD", "DO-41", "D_DO-41",
     "1N4007", 2, "diode 整流", 20,
     {"类型": "整流二极管", "耐压": 1000.0, "额定电流": 1.0},
     [("A3", 70, 0.0500, "2026-01-14", "淘宝")]),

    ("BZT52C3V3 稳压二极管", "二极管", "BZT52C3V3", "长电", "SOD-123", "D_SOD-123",
     "BZT52C3V3", 2, "diode 稳压 3.3V", 20,
     {"类型": "稳压二极管", "耐压": 3.3, "额定电流": 0.5},
     [("A3", 80, 0.0600, "2026-02-26", "立创商城")]),

    # ---------------- 三极管 / MOS ----------------
    ("S8050 NPN 三极管", "三极管/MOS", "S8050", "长电", "SOT-23", "SOT-23",
     "S8050", 4, "transistor 三极管 npn", 30,
     {"类型": "NPN 小功率", "耐压": 25.0, "额定电流": 0.5},
     [("B1", 200, 0.0300, "2026-01-14", "立创商城")]),

    ("AO3400A N沟道 MOS", "三极管/MOS", "AO3400A", "AOS", "SOT-23", "SOT-23",
     "AO3400A", 6, "mos 场效应管 n沟道", 30,
     {"类型": "N 沟道 MOS", "耐压": 30.0, "额定电流": 5.7},
     [("B1", 150, 0.0900, "2026-02-26", "立创商城")]),

    ("IRLML6402 P沟道 MOS", "三极管/MOS", "IRLML6402", "Infineon", "SOT-23", "SOT-23",
     "IRLML6402", 2, "mos 场效应管 p沟道", 20,
     {"类型": "P 沟道 MOS", "耐压": 20.0, "额定电流": 3.7},
     [("B1", 60, 0.3500, "2026-02-26", "淘宝")]),

    # ---------------- 芯片 / IC ----------------
    ("STM32F103C8T6", "芯片/IC", "STM32F103C8T6", "ST", "LQFP-48", "LQFP-48",
     "STM32F103C8T6", 1, "mcu 单片机 主控", 5,
     {"功能": "ARM Cortex-M3 主控", "工作电压": 3.3, "温度等级": "工业级"},
     [("B2", 6, 9.8500, "2026-02-26", "立创商城")]),

    ("AMS1117-3.3", "芯片/IC", "AMS1117-3.3", "AMS", "SOT-223", "SOT-223",
     "AMS1117-3.3", 2, "ldo 稳压 电源", 10,
     {"功能": "3.3V 线性稳压", "工作电压": 5.0, "温度等级": "商业级"},
     [("B2", 45, 0.4200, "2026-02-26", "立创商城")]),

    # 注意：库里的封装要和真实 BOM 写得**一致**。
    # KiCad 官方库用的是 SOIC-8 / SOIC-16，写成 SOP-8 会被判定成另一种封装
    # （两侧都是标准封装时会强校验）→ 明明有货却判缺料。
    ("LM358 双运放", "芯片/IC", "LM358DR", "TI", "SOIC-8", "SOIC-8",
     "LM358", 2, "opamp 运放 比较器", 10,
     {"功能": "双路运算放大器", "工作电压": 5.0, "温度等级": "商业级"},
     [("B2", 35, 0.2800, "2026-03-15", "立创商城")]),

    ("CH340C USB转串口", "芯片/IC", "CH340C", "沁恒", "SOIC-16", "SOIC-16",
     "CH340C", 1, "usb 串口 转接", 10,
     {"功能": "USB 转 TTL 串口", "工作电压": 3.3, "温度等级": "商业级"},
     [("B2", 20, 1.6500, "2026-04-09", "立创商城")]),

    ("TB6612FNG 电机驱动", "芯片/IC", "TB6612FNG", "Toshiba", "SSOP-24", "SSOP-24",
     "TB6612FNG", 1, "电机驱动 驱动", 4,
     {"功能": "双路直流电机驱动", "工作电压": 5.0, "温度等级": "工业级"},
     [("B2", 3, 4.7500, "2026-02-26", "淘宝")]),

    ("NE555 定时器", "芯片/IC", "NE555DR", "TI", "SOIC-8", "SOIC-8",
     "NE555", 1, "定时器 振荡", 10,
     {"功能": "定时器 / 振荡器", "工作电压": 5.0, "温度等级": "商业级"},
     [("B2", 25, 0.4500, "2026-03-15", "立创商城")]),

    # ---------------- 连接器 ----------------
    ("XH2.54 4P 卧贴插座", "连接器", "XH2.54-4P", "国产", "P2.54-4P", "P2.54-4P",
     "XH2.54-4P", 3, "conn 连接器 插座", 20,
     {"针数": 4.0, "间距": 2.54},
     [("B3", 60, 0.1600, "2026-01-14", "淘宝")]),

    ("2.54 单排排针 1x8", "连接器", "PZ254-1x8", "国产", "P2.54-1x8", "P2.54-1x8",
     "排针 1x8", 2, "conn 排针", 20,
     {"针数": 8.0, "间距": 2.54},
     [("B3", 40, 0.0800, "2026-01-14", "淘宝")]),

    ("TYPE-C 16P 母座", "连接器", "TYPE-C-16P", "国产", "TYPE-C-16P", "TYPE-C-16P",
     "TYPE-C-16P", 1, "conn usb type-c", 10,
     {"针数": 16.0, "间距": 0.5},
     [("B3", 18, 0.5500, "2026-04-09", "立创商城")]),

    # ↓ 故意没填单价
    ("2.54 单排母 1x20", "连接器", "PZ254-1x20-M", "国产", "P2.54-1x20", "P2.54-1x20",
     "排母 1x20", 2, "conn 排母", 20,
     {"针数": 20.0, "间距": 2.54},
     [("B3", 25, 0.0, "", "淘宝")]),

    # ---------------- 晶振 ----------------
    ("8MHz 无源晶振", "晶振", "HC-49S-8M", "国产", "HC-49S", "HC-49S",
     "8MHz", 1, "crystal 晶振", 10,
     {"频率": 8.0, "精度": 30.0},
     [("A3", 28, 0.2800, "2026-03-15", "立创商城")]),

    ("32.768kHz 贴片晶振", "晶振", "SMD3215-32.768K", "国产", "SMD-3215", "SMD-3215",
     "32.768kHz", 1, "crystal 晶振 rtc", 10,
     {"频率": 0.032768, "精度": 20.0},
     [("A3", 15, 0.8500, "2026-04-09", "淘宝")]),

    # ---------------- 光电器件 ----------------
    ("0805 红色 LED", "光电器件", "KT-0805R", "国星", "0805", "LED0805",
     "红色 LED", 6, "led 发光二极管 红", 50,
     {"颜色": "红", "正向电压": 2.0},
     [("A3", 40, 0.0280, "2026-01-14", "立创商城")]),

    ("0805 绿色 LED", "光电器件", "KT-0805G", "国星", "0805", "LED0805",
     "绿色 LED", 4, "led 发光二极管 绿", 50,
     {"颜色": "绿", "正向电压": 2.1},
     [("A3", 35, 0.0280, "2026-01-14", "立创商城")]),

    # ↓ 故意没填单价
    ("0805 蓝色 LED", "光电器件", "KT-0805B", "国星", "0805", "LED0805",
     "蓝色 LED", 2, "led 发光二极管 蓝", 30,
     {"颜色": "蓝", "正向电压": 3.0},
     [("A3", 20, 0.0, "", "淘宝")]),

    # ---------------- 按键 / 开关 ----------------
    ("3x4x2 贴片轻触按键", "按键/开关", "TS-3x4x2", "国产", "SW-SMD-3x4", "SW-SMD-3x4",
     "轻触按键", 6, "key 按键 轻触", 30,
     {"类型": "轻触按键", "行程": 0.25},
     [("B1", 50, 0.0500, "2026-01-14", "淘宝")]),

    ("5.8x5.8 自锁开关", "按键/开关", "SS-5.8x5.8", "国产", "SW-SMD-5.8", "SW-SMD-5.8",
     "自锁开关", 1, "switch 开关 自锁", 10,
     {"类型": "自锁开关", "行程": 2.0},
     [("B1", 12, 0.1200, "2026-01-14", "淘宝")]),
]

# 故意匹配不上的行：验证成本明细里"库里没有对得上的器件"那条路径（红底、不计价）。
# 名字和封装都特意写成库里没有的。
UNMATCHED: list[tuple[str, str, int, str]] = [
    ("ESP32-S3-WROOM-1", "RF_Module", 1, "U9"),
    ("BME280", "LGA-8", 1, "U10"),
]


def _designators(prefix: str, start: int, count: int) -> str:
    return ",".join(f"{prefix}{i}" for i in range(start, start + count))


def _bom_rows() -> list[list[str]]:
    """把器件表翻译成 BOM 行。BOM 的 Comment 用值，Manufacturer Part 用型号。"""
    rows: list[list[str]] = []
    counters: dict[str, int] = {}
    for (name, _cat, mpn, mfr, _fp, bom_fp, bom_value, bom_qty,
         _kw, _min, _params, lots) in PARTS:
        prefix = {
            "电阻": "R", "电容": "C", "电感": "L", "二极管": "D",
            "三极管/MOS": "Q", "芯片/IC": "U", "连接器": "J",
            "晶振": "Y", "光电器件": "LED", "按键/开关": "SW",
        }.get(_cat, "X")
        start = counters.get(prefix, 0) + 1
        counters[prefix] = start + bom_qty
        supplier = lots[0][4] if lots else ""
        rows.append([
            str(len(rows) + 1), str(bom_qty), bom_value,
            _designators(prefix, start, bom_qty), bom_fp,
            mpn, mfr, "", supplier,
        ])

    for value, footprint, qty, designator in UNMATCHED:
        rows.append([
            str(len(rows) + 1), str(qty), value, designator, footprint,
            "", "", "", "",
        ])
    return rows


def write_bom(path: Path) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["No.", "Quantity", "Comment", "Designator", "Footprint",
                         "Manufacturer Part", "Manufacturer", "Supplier Part", "Supplier"])
        writer.writerows(_bom_rows())
    return path


def import_into(db_path: Path, dry_run: bool = False) -> tuple[int, int, int]:
    """往一个库里灌测试器件。返回 (新增, 跳过, 无价器件数)。"""
    if not db_path.exists():
        print(f"  [跳过] 库不存在：{db_path}")
        return (0, 0, 0)

    if not dry_run:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
        shutil.copy2(db_path, backup)
        print(f"  已备份 -> {backup.name}")

    svc = Services.bootstrap(str(db_path))
    try:
        cat_by_name = {c.name: c.id for c in svc.tree.list_categories()}
        loc_by_code = {l.code: l.id for l in svc.tree.list_locations() if l.code}

        created = skipped = free = 0
        for (name, cat_name, mpn, mfr, footprint, _bom_fp, _bom_value, _bom_qty,
             keywords, min_stock, params, lots) in PARTS:

            if svc.db.query_one("SELECT id FROM part WHERE name = ?", (name,)) is not None:
                skipped += 1
                continue
            if dry_run:
                created += 1
                if lots and lots[0][2] <= 0:
                    free += 1
                continue

            cat_id = cat_by_name.get(cat_name)
            templates = {t.name: t.id for t in svc.parts.templates_for_category(cat_id)}
            part_id = svc.parts.create(
                Part(
                    name=name, category_id=cat_id, mpn=mpn, footprint=footprint,
                    keywords=f"{keywords} {TEST_MARK}".strip(), min_stock=min_stock,
                ),
                params={templates[k]: v for k, v in params.items() if k in templates},
            )
            for code, qty, price, date, supplier in lots:
                if qty <= 0:
                    continue
                svc.stock.add_lot(
                    part_id=part_id, quantity=qty,
                    location_id=loc_by_code.get(code),
                    unit_price=price, purchase_date=date,
                    supplier=supplier, manufacturer=mfr,
                )
            created += 1
            if lots and lots[0][2] <= 0:
                free += 1
        return (created, skipped, free)
    finally:
        svc.close()


def clear_from(db_path: Path) -> int:
    """按名单删掉这批测试器件，但**只删带 TEST_MARK 标记的**。

    名单里有几个器件是你导入前就有的（STM32F103C8T6、TB6612FNG…），
    只按名字删会把真实数据一起删掉。标记是唯一的凭据。
    删除会级联带走该器件的批次、参数、流水（schema 里的 ON DELETE CASCADE）。
    """
    if not db_path.exists():
        return 0
    svc = Services.bootstrap(str(db_path))
    try:
        names = [p[0] for p in PARTS]
        marks = ",".join("?" * len(names))
        rows = svc.db.query(
            f"SELECT id FROM part WHERE name IN ({marks}) AND keywords LIKE ?",
            [*names, f"%{TEST_MARK}%"],
        )
        removed = 0
        for row in rows:
            svc.parts.delete(row["id"])
            removed += 1
        return removed
    finally:
        svc.close()


def main() -> None:
    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args

    targets = [ROOT / "data" / "inventory.db",
               ROOT / "dist" / "EDMS" / "data" / "inventory.db"]
    if "--db" in sys.argv:
        targets = [Path(sys.argv[sys.argv.index("--db") + 1]).resolve()]

    if "--clear" in args:
        for db in targets:
            print(f"[清理] {db}")
            print(f"  删除 {clear_from(db)} 个测试器件")
        return

    print(f"测试器件共 {len(PARTS)} 个，其中故意不填单价的 "
          f"{sum(1 for p in PARTS if p[11] and p[11][0][2] <= 0)} 个\n")

    if not dry_run:
        bom = write_bom(ROOT / "Bom" / BOM_NAME)
        print(f"[BOM] 已写出 {bom.relative_to(ROOT)}"
              f"（{len(_bom_rows())} 行，其中 {len(UNMATCHED)} 行故意匹配不上）\n")

    for db in targets:
        rel = db.relative_to(ROOT) if db.is_relative_to(ROOT) else db
        print(f"[导入] {rel}")
        created, skipped, free = import_into(db, dry_run)
        print(f"  新增 {created} 个，跳过已存在 {skipped} 个，其中无价 {free} 个\n")

    if dry_run:
        print("（--dry-run：没有动任何库）")


if __name__ == "__main__":
    main()
