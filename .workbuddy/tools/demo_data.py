"""灌一份演示数据，方便你打开界面时看到真实的样子。

    .venv/Scripts/python.exe .workbuddy/tools/demo_data.py          # 灌入
    .venv/Scripts/python.exe .workbuddy/tools/demo_data.py --clear  # 清空全部器件后重灌

它**只动器件、库存、流水**，不碰分类和位置（那两棵是 seed 建的）。
重名器件会被跳过，所以重复执行不会堆出重复数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.domain import Services                      # noqa: E402
from app.domain.models import Part                   # noqa: E402

# (名称, 分类名, 型号, 厂商, 封装, 关键词, 预警阈值,
#  参数名->值, [(位置短码, 数量, 单价, 购买日期, 供应商)])
DEMO: list[tuple] = [
    ("10kΩ 0603 1% 电阻", "电阻", "RC0603FR-0710KL", "Yageo", "0603", "res 电阻", 100,
     {"阻值": 10000.0, "精度": 1.0, "功率": 0.1},
     [("A1", 500, 0.0086, "2026-03-15", "立创商城"),
      ("A1", 200, 0.0079, "2026-08-20", "立创商城")]),

    ("4.7kΩ 0603 1% 电阻", "电阻", "RC0603FR-074K7L", "Yageo", "0603", "res 电阻 上拉", 50,
     {"阻值": 4700.0, "精度": 1.0, "功率": 0.1},
     [("A1", 180, 0.0080, "2026-03-15", "立创商城")]),

    ("100Ω 0603 1% 电阻", "电阻", "RC0603FR-07100RL", "Yageo", "0603", "res 电阻", 50,
     {"阻值": 100.0, "精度": 1.0, "功率": 0.1},
     [("A1", 12, 0.0080, "2026-03-15", "立创商城")]),

    ("1kΩ 0805 1% 电阻", "电阻", "RC0805FR-071KL", "Yageo", "0805", "res 电阻 下拉", 50,
     {"阻值": 1000.0, "精度": 1.0, "功率": 0.125},
     [("A1", 0, 0.0, "", "")]),

    ("100nF 0603 50V X7R 电容", "电容", "CL10B104KB8NNNC", "Samsung", "0603", "cap 电容 104", 200,
     {"容值": 100.0, "耐压": 50.0, "精度": 10.0, "介质": "X7R"},
     [("A2", 800, 0.0125, "2026-03-15", "立创商城"),
      ("A2", 400, 0.0118, "2026-07-02", "立创商城")]),

    ("10uF 0805 25V X5R 电容", "电容", "CL21A106KAYNNNE", "Samsung", "0805", "cap 电容", 100,
     {"容值": 10000.0, "耐压": 25.0, "精度": 10.0, "介质": "X5R"},
     [("A2", 220, 0.085, "2026-05-11", "立创商城")]),

    ("22pF 0603 晶振负载电容", "电容", "0603CG220J500NT", "FH", "0603", "cap 电容 晶振", 20,
     {"容值": 22.0, "耐压": 50.0, "精度": 5.0, "介质": "C0G"},
     [("A2", 8, 0.031, "2025-11-08", "淘宝")]),

    ("STM32F103C8T6", "芯片/IC", "STM32F103C8T6", "ST", "LQFP-48", "mcu 单片机 主控", 5,
     {"功能": "ARM Cortex-M3 主控", "工作电压": 3.3, "温度等级": "工业级"},
     [("B2", 6, 9.85, "2026-02-26", "立创商城")]),

    ("AMS1117-3.3", "芯片/IC", "AMS1117-3.3", "AMS", "SOT-223", "ldo 稳压 电源", 10,
     {"功能": "3.3V 线性稳压", "工作电压": 5.0, "温度等级": "商业级"},
     [("B2", 45, 0.42, "2026-02-26", "立创商城")]),

    ("MPU6050", "传感器", "MPU-6050", "InvenSense", "QFN-24", "imu 陀螺仪 加速度计", 5,
     {"类型": "六轴姿态传感器", "工作电压": 3.3},
     [("B2", 4, 6.30, "2026-02-26", "淘宝")]),

    ("TB6612FNG", "芯片/IC", "TB6612FNG", "Toshiba", "SSOP-24", "电机驱动 驱动", 4,
     {"功能": "双路直流电机驱动", "工作电压": 5.0, "温度等级": "工业级"},
     [("B2", 3, 4.75, "2026-02-26", "淘宝")]),

    ("NRF24L01+ 2.4G 模块", "模块", "NRF24L01P", "Nordic", "DIP-8", "无线 2.4g 通信", 4,
     {"功能": "2.4GHz 无线收发", "工作电压": 3.3},
     [("B3", 5, 3.20, "2026-04-09", "淘宝")]),

    ("0.96 寸 OLED 模块", "模块", "SSD1306-0.96", "中景园", "DIP-4", "oled 显示 屏幕", 2,
     {"功能": "128x64 单色显示", "工作电压": 3.3},
     [("B3", 2, 8.90, "2026-04-09", "淘宝")]),

    ("XH2.54 4P 卧贴插座", "连接器", "XH2.54-4P", "国产", "P2.54-4P", "conn 连接器 插座", 20,
     {"针数": 4.0, "间距": 2.54},
     [("B1", 60, 0.16, "2026-01-14", "淘宝")]),

    ("1N4148W 开关二极管", "二极管", "1N4148W", "长电", "SOD-123", "diode 二极管 开关", 50,
     {"类型": "高速开关二极管", "耐压": 100.0, "额定电流": 0.15},
     [("A3", 150, 0.021, "2026-01-14", "立创商城")]),

    ("SS34 肖特基二极管", "二极管", "SS34", "长电", "DO-214AC(SMA)", "diode 肖特基 整流", 30,
     {"类型": "肖特基", "耐压": 40.0, "额定电流": 3.0},
     [("A3", 26, 0.093, "2026-01-14", "立创商城")]),

    ("AO3400 N沟道 MOS", "三极管/MOS", "AO3400A", "AOS", "SOT-23", "mos 场效应管 开关", 30,
     {"类型": "N沟道 MOSFET", "耐压": 30.0, "额定电流": 5.7},
     [("A3", 88, 0.055, "2026-01-14", "立创商城")]),

    ("8MHz 无源晶振", "晶振", "HC-49S-8M", "国产", "HC-49S", "crystal 晶振 时钟", 10,
     {"频率": 8.0, "精度": 30.0},
     [("A3", 9, 0.28, "2025-11-08", "淘宝")]),

    ("6x6x5 轻触按键", "按键/开关", "TS-1088-5", "国产", "6x6x5", "key 按键 开关", 20,
     {"类型": "立式轻触开关", "行程": 0.25},
     [("C", 100, 0.045, "2026-01-14", "淘宝")]),

    ("0805 红色 LED", "光电器件", "KT-0805R", "国星", "0805", "led 发光二极管 红", 50,
     {"颜色": "红", "正向电压": 2.0},
     [("A3", 40, 0.028, "2026-01-14", "立创商城")]),
]


def main() -> None:
    clear = "--clear" in sys.argv
    svc = Services.bootstrap()

    if clear:
        n = svc.db.query_one("SELECT COUNT(*) AS n FROM part")["n"]
        svc.db.execute("DELETE FROM part")
        print(f"已清空 {n} 个旧器件\n")

    cat_by_name = {c.name: c.id for c in svc.tree.list_categories()}
    loc_by_code = {l.code: l.id for l in svc.tree.list_locations() if l.code}

    created = skipped = 0
    for (name, cat_name, mpn, mfr, footprint, keywords, min_stock,
         params, lots) in DEMO:

        existing = svc.db.query_one("SELECT id FROM part WHERE name = ?", (name,))
        if existing is not None:
            skipped += 1
            continue

        cat_id = cat_by_name.get(cat_name)
        templates = {t.name: t.id for t in svc.parts.templates_for_category(cat_id)}

        part_id = svc.parts.create(
            Part(
                name=name,
                category_id=cat_id,
                mpn=mpn,
                footprint=footprint,
                keywords=keywords,
                min_stock=min_stock,
            ),
            params={templates[k]: v for k, v in params.items() if k in templates},
        )

        for code, qty, price, date, supplier in lots:
            if qty <= 0:
                continue
            svc.stock.add_lot(
                part_id=part_id,
                quantity=qty,
                location_id=loc_by_code.get(code),
                unit_price=price,
                purchase_date=date,
                supplier=supplier,
                # 厂商记在批次上，不在器件上
                manufacturer=mfr,
            )
        created += 1

    total = svc.db.query_one("SELECT COUNT(*) AS n FROM part")["n"]
    lots_total = svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"]
    print(f"新增 {created} 个器件，跳过已存在 {skipped} 个")
    print(f"当前共 {total} 个器件、{lots_total} 个库存批次")
    print(f"库存总货值 ¥{svc.stock.inventory_value():,.2f}")
    http = svc.search.low_stock()
    print(f"需要补货：{len(http)} 种")
    for item in http:
        print(f"    - {item.name:<28} 库存 {item.total_qty:>5}  阈值 {item.min_stock}")

    svc.close()


if __name__ == "__main__":
    main()
