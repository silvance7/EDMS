"""数据层冒烟测试：不开界面，直接验证 P0 + P1。

跑法（在项目根目录）：
    .venv/Scripts/python.exe .workbuddy/tools/smoke_test.py

它会在临时目录建一个独立的库，跑完就删，绝不碰你 data/inventory.db 里的真实数据。
每一段都带断言，任何一条不成立都会以非零码退出。
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 让脚本能直接 import app.*（不依赖从哪个目录调用）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.domain import InsufficientStockError, Services          # noqa: E402
from app.domain.bom import (                                     # noqa: E402
    BomLine,
    BomMatcher,
    _keyword_tokens,
    build_cost,
    is_standard_footprint,
    parse_value,
    primary_footprint,
    read_bom,
    values_equal,
)
from app.domain.models import Part, SearchQuery                  # noqa: E402
from app.domain.stock_service import BulkInsufficientStockError  # noqa: E402
from app.storage.db import Database                              # noqa: E402
from app.storage.migrations import get_version, run_migrations   # noqa: E402

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        print(f"  [FAIL] {label}  {detail}")
        raise SystemExit(1)
    PASSED += 1
    print(f"  [ ok ] {label}{('  -> ' + detail) if detail else ''}")


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="edms_smoke_"))
    db_path = tmpdir / "test.db"
    print(f"临时库：{db_path}\n")

    svc = Services.bootstrap(str(db_path))

    # 把临时库登记到退出时清理。
    # atexit 是 LIFO 的：先注册"删目录"、后注册"关连接"，退出时会先关连接再删目录。
    # 顺序反了的话 Windows 会因为文件被占用而删不掉，留下垃圾（之前就漏过 11 个）。
    atexit.register(shutil.rmtree, tmpdir, ignore_errors=True)
    atexit.register(svc.close)

    # ---------------------------------------------------------------- P0
    print("P0 · 建库与默认数据")
    tables = svc.db.query(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    )
    names = {r["name"] for r in tables}
    for expected in ("category", "location", "part", "part_param", "stock_lot", "stock_log",
                     "param_template", "v_part_overview"):
        check(f"存在 {expected}", expected in names)

    check("默认分类已灌入", len(svc.tree.list_categories()) >= 10,
          f"{len(svc.tree.list_categories())} 个")
    check("默认位置已灌入", len(svc.tree.list_locations()) >= 8,
          f"{len(svc.tree.list_locations())} 个")

    journal = svc.db.query_one("PRAGMA journal_mode")
    check("WAL 模式已开启", str(journal[0]).lower() == "wal", str(journal[0]))
    fk = svc.db.query_one("PRAGMA foreign_keys")
    check("外键约束已开启", int(fk[0]) == 1, str(fk[0]))

    # ---------------------------------------------------------------- P1 参数模板继承
    print("\nP1 · 分类与参数模板继承")
    resistor = next(c for c in svc.tree.list_categories() if c.name == "电阻")
    tpl_names = [t.name for t in svc.parts.templates_for_category(resistor.id)]
    check("电阻分类拿到模板", set(tpl_names) == {"阻值", "精度", "功率", "耐压"}, str(tpl_names))

    # 建一个子分类，验证它能继承父分类的模板
    sub_id = svc.tree.create_category("贴片电阻", parent_id=resistor.id)
    sub_tpls = [t.name for t in svc.parts.templates_for_category(sub_id)]
    check("子分类继承父分类模板", set(sub_tpls) == {"阻值", "精度", "功率", "耐压"}, str(sub_tpls))
    svc.parts.add_template(sub_id, "温漂", "ppm/°C", "num")
    sub_tpls2 = [t.name for t in svc.parts.templates_for_category(sub_id)]
    check("子分类可追加自己的模板", "温漂" in sub_tpls2 and len(sub_tpls2) == 5, str(sub_tpls2))

    # ---------------------------------------------------------------- P1 建器件
    print("\nP1 · 建器件 + 参数 + SI 前缀展开")
    tpl_by_name = {t.name: t.id for t in svc.parts.templates_for_category(sub_id)}
    part_id = svc.parts.create(
        Part(
            name="10kΩ 0603 1% 电阻",
            category_id=sub_id,
            mpn="RC0603FR-0710KL",
            footprint="0603",
            keywords="res 电阻",
            min_stock=50,
        ),
        params={
            tpl_by_name["阻值"]: 10000.0,
            tpl_by_name["精度"]: 1.0,
            tpl_by_name["功率"]: 0.1,
        },
    )
    check("器件已创建", part_id > 0, f"id={part_id}")

    created = svc.parts.get(part_id)
    check("器件字段正确", created.name == "10kΩ 0603 1% 电阻" and created.footprint == "0603")

    params = svc.parts.get_params(part_id)
    check("参数已保存", len(params) == 3, f"{len(params)} 条")
    value_display = {p.name: p.display for p in params}
    check("参数显示带单位", value_display.get("阻值") == "10000 Ω", str(value_display))
    check("分类路径写入检索列", "电阻 / 贴片电阻" in created.search_text)
    check("SI 前缀展开 10k", "10k" in created.search_text,
          created.search_text[:110])
    check("SI 前缀展开带单位 10kΩ", "10kω" in created.search_text)

    # ---------------------------------------------------------------- P1 入库
    print("\nP1 · 入库（批次 + 合并）")
    a1 = next(l for l in svc.tree.list_locations() if l.code == "A1")
    b2 = next(l for l in svc.tree.list_locations() if l.code == "B2")

    svc.stock.add_lot(part_id, 100, a1.id, unit_price=0.008,
                      purchase_date="2026-03-15", supplier="立创商城")
    svc.stock.add_lot(part_id, 50, a1.id, unit_price=0.008,
                      purchase_date="2026-03-15", supplier="立创商城")
    svc.stock.add_lot(part_id, 40, b2.id, unit_price=0.012,
                      purchase_date="2026-09-02", supplier="淘宝")

    lots = svc.stock.lots(part_id)
    check("相同条件的采购已自动合并", len(lots) == 2, f"{len(lots)} 个批次")
    check("总库存 = 100+50+40", svc.stock.total_quantity(part_id) == 190,
          str(svc.stock.total_quantity(part_id)))

    # ---------------------------------------------------------------- P1 检索
    print("\nP1 · 检索")
    check("按名称关键词命中", len(svc.search.search(SearchQuery(keyword="10k"))) == 1)
    check("多词 AND 命中", len(svc.search.search(SearchQuery(keyword="10k 0603"))) == 1)
    check("多词 AND 不误命中", len(svc.search.search(SearchQuery(keyword="10k 0805"))) == 0)
    check("按封装筛选命中", len(svc.search.search(SearchQuery(footprint="0603"))) == 1)
    check("按分类树筛选命中", len(svc.search.search(SearchQuery(category_id=resistor.id))) == 1)
    check("按位置筛选命中", len(svc.search.search(SearchQuery(location_id=b2.id))) == 1)
    check("按位置筛选不误命中", len(svc.search.search(SearchQuery(location_id=a1.id))) == 1)

    overview = svc.search.quick("10k")[0]
    check("视图聚合总数正确", overview.total_qty == 190, str(overview.total_qty))
    check("位置分布已拼装", "A1×150" in overview.location_summary, overview.location_summary)
    check("分类路径已拼装", overview.category_path == "电阻 / 贴片电阻", overview.category_path)
    check("未触及低库存阈值", overview.is_low_stock is False)

    # 参数范围筛选：10000 Ω 落在 10k~100k 之间
    ranged = svc.search.search(
        SearchQuery(param_ranges=[("阻值", 1000.0, 100000.0)])
    )
    check("参数范围筛选命中", len(ranged) == 1)
    out_of_range = svc.search.search(
        SearchQuery(param_ranges=[("阻值", 100000.0, 200000.0)])
    )
    check("参数范围筛选正确排除", len(out_of_range) == 0)

    # ---------------------------------------------------------------- P1 出库
    print("\nP1 · 出库（FIFO + 事务）")
    # 当前批次构成：A1 有 150（两笔采购已合并），B2 有 40
    taken = svc.stock.withdraw(part_id, 120, ref="平衡车 v1")
    check("FIFO 先扣早期批次", taken[0][0] == lots[0].id, str(taken))
    check("单批足够时不碰其它批次", len(taken) == 1, str(taken))
    check("出库后总库存正确", svc.stock.total_quantity(part_id) == 70,
          str(svc.stock.total_quantity(part_id)))

    # A1 只剩 30，再出 50 必须跨到 B2
    taken2 = svc.stock.withdraw(part_id, 50, ref="跨批次测试")
    check("FIFO 跨批次扣减", len(taken2) == 2, str(taken2))
    check("跨批次顺序正确",
          taken2[0][0] == lots[0].id and taken2[1][0] == lots[1].id, str(taken2))
    check("跨批次后总库存 = 20", svc.stock.total_quantity(part_id) == 20,
          str(svc.stock.total_quantity(part_id)))

    try:
        svc.stock.withdraw(part_id, 9999, ref="越界测试")
        check("库存不足应当抛异常", False)
    except InsufficientStockError as exc:
        check("库存不足抛异常且不扣减", True, str(exc))
    check("失败后库存未被改动", svc.stock.total_quantity(part_id) == 20,
          str(svc.stock.total_quantity(part_id)))

    low = svc.search.search(SearchQuery(keyword="10k"))[0]
    check("剩余 20 已低于阈值 50，触发预警",
          low.is_low_stock is True and low.stock_state == "low",
          f"总库存 {low.total_qty}, 阈值 {low.min_stock}")
    check("低库存列表能查到", len(svc.search.low_stock()) == 1)

    # ---------------------------------------------------------------- 流水
    print("\nP1 · 库存流水")
    logs = svc.stock.logs(part_id)
    check("流水已记录", len(logs) >= 6, f"{len(logs)} 条")
    check("流水含负数（出库）", any(l.delta < 0 for l in logs))
    check("流水含正数（入库）", any(l.delta > 0 for l in logs))
    delta_sum = sum(l.delta for l in logs)
    check("流水差额 == 实际库存（账实相符）",
          delta_sum == svc.stock.total_quantity(part_id),
          f"流水和 {delta_sum} vs 库存 {svc.stock.total_quantity(part_id)}")

    # ---------------------------------------------------------------- 级联与备份
    print("\nP1 · 级联删除与热备份")
    backup = svc.db.backup_to(tmpdir / "backup.db")
    check("热备份已生成", backup.exists() and backup.stat().st_size > 0,
          f"{backup.stat().st_size} 字节")

    svc.parts.delete(part_id)
    check("删除器件后无残留批次",
          svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot WHERE part_id = ?",
                           (part_id,))["n"] == 0)
    check("删除器件后无残留流水",
          svc.db.query_one("SELECT COUNT(*) AS n FROM stock_log WHERE part_id = ?",
                           (part_id,))["n"] == 0)
    check("删除器件后无残留参数",
          svc.db.query_one("SELECT COUNT(*) AS n FROM part_param WHERE part_id = ?",
                           (part_id,))["n"] == 0)

    # ---------------------------------------------------------------- BOM 归一化
    print("\nBOM · 数值与封装归一化")

    check("0.1uF 与 100nF 等价（单位换算）",
          values_equal(parse_value("0.1uF"), parse_value("100nF")))
    check("22UF 与 22uF 等价（大小写）",
          values_equal(parse_value("22UF"), parse_value("22uF")))
    check("1k 与 1K 等价（大小写）",
          values_equal(parse_value("1k"), parse_value("1K")))
    check("100nF 与 10uF 不等价",
          not values_equal(parse_value("100nF"), parse_value("10uF")))
    check("型号不会被误当成数值", parse_value("1N4148W") is None)
    check("带单位的 10kΩ 解析正确", str(parse_value("10kΩ")) == "10000R")

    check("C0603 -> 0603", primary_footprint("C0603") == "0603")
    check("R0805 -> 0805", primary_footprint("R0805") == "0805")
    check("LED_0603 -> 0603（前缀不是 R/C/L 也能认）",
          primary_footprint("LED_0603") == "0603")
    check("LQFP-48_L7.0-... -> lqfp-48",
          primary_footprint("LQFP-48_L7.0-W7.0-P0.50-LS9.0-BL") == "lqfp-48")
    # 回归：曾经把 SOT-23-5 截成 sot-23，导致 3/5/6 脚全塌成一种封装
    check("SOT-23-5 保住脚数（回归）",
          primary_footprint("SOT-23-5_L3.0-W1.7-P0.95-LS2.8-BL") == "sot-23-5")
    check("SOT-23-3 保住脚数（回归）",
          primary_footprint("SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR") == "sot-23-3")
    check("SOT-23 不被误扩成 sot-23-5",
          primary_footprint("SOT-23_L2.9-W1.3-P1.90-LS2.4-BR") == "sot-23")

    # 回归：曾经用 m3 / 4p 这种两字符 token 去检索，命中 STM32 的 Cortex-M3
    check("M3螺丝 的 m3 被丢弃（回归）", _keyword_tokens("M3螺丝") == ["螺丝"])
    check("中英混写能切开", _keyword_tokens("nrf24l01模块") == ["nrf24l01", "模块"])
    check("英制尺寸不当检索词", "0603" not in _keyword_tokens("LED_0603-G"))

    # ---------------------------------------------------------------- BOM 解析
    print("\nBOM · 文件解析")
    bom_csv = tmpdir / "test_bom.csv"
    bom_csv.write_text(
        "No.,Quantity,Comment,Designator,Footprint,Value,"
        "Manufacturer Part,Manufacturer,Supplier Part,Supplier\n"
        "1,3,10k,R1 R2 R3,R0603,10K,RC0603FR-0710KL,Yageo,C9900012714,LCSC\n"
        "2,2,100nF,C1 C2,C0603,100nF,,,,\n"
        "3,5,STM32F103C8T6,U1,LQFP-48_L7.0-W7.0-P0.50-LS9.0-BL,,"
        "STM32F103C8T6,ST,C8734,LCSC\n"
        "4,1,不存在的料,X1,XXX-1,,,,,\n",
        encoding="utf-8-sig",
    )
    bom_lines = read_bom(bom_csv)
    check("CSV 解析出 4 行", len(bom_lines) == 4, f"{len(bom_lines)} 行")
    check("数量解析为整数", bom_lines[0].qty == 3, str(bom_lines[0].qty))
    check("位号原样保留", bom_lines[0].designators == "R1 R2 R3")
    check("供应商编号解析", bom_lines[0].supplier_part == "C9900012714")

    # ---------------------------------------------------------------- BOM 打分匹配
    print("\nBOM · 打分匹配")
    # 电容的容值必须存在「容值(nF)」模板上。存进电阻的「阻值(Ω)」模板
    # 会解析不出数值，那样连"参数核对"都做不了 —— 自己再造一个模板来测。
    # create_category 返回的是 id（int），不是 Category 对象
    cap_cat = svc.tree.create_category("测试用电容")
    cap_tpl = svc.parts.add_template(cap_cat, "容值", "nF", "num")

    bom_r = svc.parts.create(
        Part(name="10kΩ 0603 1% 电阻", category_id=resistor.id, footprint="0603",
             mpn="RC0603FR-0710KL", keywords="C9900012714", min_stock=10),
        {tpl_by_name["阻值"]: 10000.0},
    )
    bom_c = svc.parts.create(
        Part(name="100nF 0603 50V X7R 电容", category_id=cap_cat, footprint="0603"),
        {cap_tpl: 100.0},                    # 100 nF = 1e-7 F
    )
    bom_u = svc.parts.create(
        Part(name="STM32F103C8T6", category_id=resistor.id, footprint="LQFP-48",
             mpn="STM32F103C8T6", keywords="C8734"))
    bom_ok = svc.parts.create(
        Part(name="10uF 0603 25V 电容", category_id=cap_cat, footprint="0603"),
        {cap_tpl: 10000.0},                  # 10000 nF = 10 uF
    )

    for pid_, qty_ in ((bom_r, 50), (bom_c, 50), (bom_u, 20), (bom_ok, 50)):
        svc.stock.add_lot(pid_, qty_, a1.id, unit_price=0.01, purchase_date="2026-01-01")

    matcher = BomMatcher(svc)
    matches = matcher.match(bom_lines)

    top = matches[0].candidates[0]
    check("供应商编号命中排第一", top.part_id == bom_r)
    check("供应商编号命中 = 完全匹配", top.exact is True)
    check("完全匹配的默认勾选", matches[0].ready is True)
    check("匹配依据里写明供应商编号",
          any("供应商编号" in item for item in top.evidence), str(top.evidence))

    top_c = matches[1].candidates[0]
    check("数值+封装命中 100nF", top_c.part_id == bom_c)
    check("100nF 是完全匹配", top_c.exact is True)
    check("完全匹配没有 gaps", top_c.gaps == [], str(top_c.gaps))

    top_u = matches[2].candidates[0]
    check("厂商型号命中 STM32", top_u.part_id == bom_u)
    check("型号完全一致 = 完全匹配", top_u.exact is True)

    check("对不上的行没有候选", matches[3].candidates == [])
    check("对不上的行状态是 missing", matches[3].state == "missing")

    # --- 弱参数不吻合：只标警告，不排除 ---
    weak = svc.parts.create(
        Part(name="贴片电容 0.1微法", category_id=cap_cat, footprint="0603"),
        {cap_tpl: 100.0})

    def make_line(index: int, comment: str, footprint: str, value: str = "") -> BomLine:
        return BomLine(index=index, qty=1, comment=comment, designators="X1",
                       footprint=footprint, value=value, mpn="",
                       manufacturer="", supplier_part="", supplier="")

    matcher = BomMatcher(svc)
    weak_match = matcher.match_line(make_line(99, "0.1uF", "C0603", "0.1uF"))
    weak_hit = next((c for c in weak_match.candidates if c.part_id == weak), None)
    check("数值一致但名称对不上 -> 仍进候选", weak_hit is not None)
    check("弱参数不吻合 -> 不是完全匹配（要标警告）",
          weak_hit is not None and weak_hit.exact is False)
    check("警告原因说明了名称对不上",
          weak_hit is not None and any("名称" in g for g in weak_hit.gaps),
          str(weak_hit.gaps if weak_hit else None))
    check("弱参数的候选默认不勾选", weak_match.ready is False)

    # --- 库存侧数据缺失：不能判死，要标明"无法核对" ---
    # 这里直接测 _strong_gap / _confirm_gaps：要构造一个"既没记参数、
    # 名字里也没数字"的器件，走完整流程反而要额外造索引条件，测不准。
    no_param = svc.parts.create(
        Part(name="贴片电容 常用", category_id=cap_cat, footprint="0603"))
    matcher = BomMatcher(svc)
    np_line = make_line(96, "0.1uF", "C0603", "0.1uF")
    np_key = parse_value(np_line.value_text, np_line.footprint)
    check("库里没记数值时不判死（回归）",
          matcher._strong_gap(np_line, no_param, np_key) is None,
          "一个数值参数都没记 -> 是'无法核对'，不是'参数不符'")
    check("但要标出'无法核对'让用户去补录",
          any("没记数值" in g for g in
              matcher._confirm_gaps(np_line, no_param, np_key, {"0.1"})),
          "不能默默当成对上了")

    # --- 强参数不符：直接排除（对齐立创"封装不符不列入结果"）---
    mism = svc.parts.create(
        Part(name="10uF 电容测试", category_id=cap_cat, footprint="0805"),
        {cap_tpl: 10000.0})
    matcher = BomMatcher(svc)
    result_10u = matcher.match_line(make_line(98, "10uF", "C0603"))
    scored = {c.part_id: c.score for c in result_10u.candidates}
    check("封装不符的候选被直接排除（回归）", mism not in scored,
          f"候选 {len(scored)} 个")
    check("封装一致的候选仍在列", bom_ok in scored)
    check("被排除的原因作为 near_miss 提示出来",
          any("封装不符" in item for item in result_10u.near_miss),
          str(result_10u.near_miss))

    # 数值不符同理
    result_v = matcher.match_line(make_line(97, "47uF", "C0603"))
    check("参数不符的也不进候选",
          all(c.part_id != bom_ok for c in result_v.candidates),
          f"候选 {len(result_v.candidates)} 个")

    # --- 自定义封装：算法认不出来，不能强校验（否则把有货判成缺料）---
    check("标准封装判定正确",
          is_standard_footprint("0603") and is_standard_footprint("lqfp-48")
          and is_standard_footprint("sot-23-5") and is_standard_footprint("hc-49s"),
          "0603 / lqfp-48 / sot-23-5 / hc-49s 都该算标准封装")
    check("自定义封装不判为标准",
          not is_standard_footprint("0.96oled")
          and not is_standard_footprint("smd-pcb")
          and not is_standard_footprint("hdr-th"),
          "KiCad 里自己画的封装名不该算标准封装")

    custom = svc.parts.create(
        Part(name="0.96 寸 OLED 模块", category_id=resistor.id, footprint="DIP-4"))
    matcher = BomMatcher(svc)
    custom_cands = {c.part_id: c
                    for c in matcher.match_line(
                        make_line(95, "0.96OLED模块_4P", "0.96OLED_4P")).candidates}
    check("自定义封装不判死，仍进候选（回归）", custom in custom_cands,
          f"候选 {len(custom_cands)} 个")
    check("但会标出封装对不上、要人工确认",
          custom in custom_cands
          and any("封装对不上" in g for g in custom_cands[custom].gaps),
          str(custom_cands[custom].gaps) if custom in custom_cands else "不在候选里")
    # 回归：曾经因为"假设强参数已经卡过"而给封装对不上的候选
    # 打上"封装一致"的依据，自相矛盾
    check("封装对不上时不谎报「封装一致」（回归）",
          custom in custom_cands
          and not any("封装一致" in e for e in custom_cands[custom].evidence),
          str(custom_cands[custom].evidence) if custom in custom_cands else "")

    # ---------------------------------------------------------------- 成本折算
    print("\nBOM · 成本折算（这套板子要花多少钱）")
    overview = {r.id: r for r in svc.search.search(SearchQuery(limit=5000))}
    report = build_cost(matches, overview.get)

    # bom_r / bom_c / bom_u 的单价都是 0.01，数量分别是 3 / 2 / 5
    check("总价 = Σ(数量 × 均价)",
          abs(report.total - (3 + 2 + 5) * 0.01) < 1e-9, f"{report.total:.6f}")
    check("有价行小计 = 数量 × 单价",
          abs(report.lines[0].subtotal - 3 * 0.01) < 1e-9,
          f"{report.lines[0].subtotal:.6f}")
    check("有价行可改价（有在库批次）", report.lines[0].editable is True)
    check("没匹配上的行不算钱", report.lines[3].matched is False)
    check("缺料也算「算不齐」", report.complete is False)
    check("未匹配的行置顶", report.ordered[0].index == matches[3].line.index)

    # 没记价格的器件：入库时忘了填单价，落到库里就是 0
    bom_np = svc.parts.create(
        Part(name="1nF 0603 50V 电容", category_id=cap_cat, footprint="0603"),
        {cap_tpl: 1.0},
    )
    svc.stock.add_lot(bom_np, 30, a1.id, unit_price=0.0, purchase_date="2026-01-01")
    np_line = make_line(96, "1nF", "C0603")
    np_match = BomMatcher(svc).match_line(np_line)
    check("无价器件也能匹配上", np_match.part_id == bom_np)

    overview2 = {r.id: r for r in svc.search.search(SearchQuery(limit=5000))}
    report2 = build_cost([np_match] + matches, overview2.get)
    check("没记价格的行被判为「待补价」",
          len(report2.missing) == 1 and report2.missing[0].part_id == bom_np,
          str(len(report2.missing)))
    check("待补价的行置顶", report2.ordered[0].index == 96)
    check("有它的情况下总价仍是「不完整」", report2.complete is False)
    check("待补价行的小计按 0 计", report2.ordered[0].subtotal == 0.0)
    check("待补价但有库存 → 能改价", report2.ordered[0].editable is True)

    # 关键回归：**批次数 > 0 但在库总量为 0** 的器件，改价没有落点。
    # 判据用的是在库总量，不是批次数 —— 用 lot_count 会出现"显示可改、实际改不动"。
    zero = svc.parts.create(
        Part(name="已清空的电阻", category_id=resistor.id, footprint="0603"),
        {tpl_by_name["阻值"]: 0.0},
    )
    z_lot = svc.stock.add_lot(zero, 5, a1.id, unit_price=0.02,
                              purchase_date="2026-01-01")
    svc.stock.adjust(z_lot, 0)
    overview3 = {r.id: r for r in svc.search.search(SearchQuery(limit=5000))}
    line_z = build_cost([BomMatcher(svc).match_line(make_line(97, "已清空的电阻", "R0603"))],
                        overview3.get).lines[0]
    check("批次还在但数量为 0 → 不算有库存", line_z.in_stock == 0)
    check("没有在库批次就不能在这儿改价（改了也没落点）", line_z.editable is False)

    # ---------------------------------------------------------------- 按器件改单价
    print("\nBOM · 按器件改单价（事后补价）")
    two = svc.parts.create(
        Part(name="双批次电阻", category_id=resistor.id, footprint="0603"),
        {tpl_by_name["阻值"]: 2000.0},
    )
    svc.stock.add_lot(two, 100, a1.id, unit_price=0.01, purchase_date="2026-01-01")
    svc.stock.add_lot(two, 300, a1.id, unit_price=0.03, purchase_date="2026-02-01")

    touched = svc.stock.set_unit_price(two, 0.02)
    check("改价写到所有在库批次", touched == 2, str(touched))
    prices = sorted(r["unit_price"] for r in
                    svc.db.query("SELECT unit_price FROM stock_lot WHERE part_id = ?", (two,)))
    check("两个批次单价都变成新值", prices == [0.02, 0.02], str(prices))
    check("数量一个没动", svc.stock.total_quantity(two) == 400)
    check("改价不写流水（改价不是出入库，别污染「库存可重建」）",
          svc.db.query_one("SELECT COUNT(*) AS n FROM stock_log WHERE part_id = ?",
                           (two,))["n"] == 2)
    overview4 = {r.id: r for r in svc.search.search(SearchQuery(limit=5000))}
    check("改价后均价就是新值", abs(overview4[two].avg_price - 0.02) < 1e-9,
          f"{overview4[two].avg_price}")
    check("没有在库批次时改价影响 0 条", svc.stock.set_unit_price(zero, 0.02) == 0)
    try:
        svc.stock.set_unit_price(two, -1.0)
        check("负单价要拦", False)
    except ValueError:
        check("负单价要拦", True)

    # ---------------------------------------------------------------- 批量出库
    print("\nBOM · 批量出库（全有才出）")
    before = svc.stock.total_quantity(bom_r)

    try:
        svc.stock.withdraw_many([(bom_r, before + 1, "test")])
        check("库存不足应当整批取消", False)
    except BulkInsufficientStockError as exc:
        check("库存不足抛批量异常", True, f"{len(exc.shortages)} 项不足")
    check("整批取消后库存一颗没扣", svc.stock.total_quantity(bom_r) == before,
          f"{before} -> {svc.stock.total_quantity(bom_r)}")

    # 关键回归：单行都够、加起来不够 —— 必须汇总校验
    try:
        svc.stock.withdraw_many([(bom_r, before, "a"), (bom_r, 1, "b")])
        check("跨行汇总后不足也要拦", False)
    except BulkInsufficientStockError as exc:
        check("跨行汇总后不足也要拦", True, str(exc.shortages))
    check("汇总校验失败后库存未变", svc.stock.total_quantity(bom_r) == before)

    results = svc.stock.withdraw_many([(bom_r, 2, "bom-a"), (bom_r, 3, "bom-b")])
    check("批量出库成功", len(results) == 2)
    check("同一器件多行数量正确汇总", svc.stock.total_quantity(bom_r) == before - 5,
          f"{before} -> {svc.stock.total_quantity(bom_r)}")
    check("流水备注带上了来源",
          svc.db.query_one(
              "SELECT COUNT(*) AS n FROM stock_log WHERE ref = 'bom-a'")["n"] >= 1)

    # ---------------------------------------------------------------- 未匹配建档
    print("\nBOM · 未匹配行一键建档")
    missing_line = matches[3].line
    new_pid = svc.parts.create(
        Part(name=missing_line.label, footprint=missing_line.footprint,
             mpn=missing_line.mpn,
             keywords=missing_line.supplier_part,
             min_stock=missing_line.qty))
    new_part = svc.parts.get(new_pid)
    check("建档后预警阈值 = 需求量", new_part.min_stock == missing_line.qty,
          f"{new_part.min_stock}")
    check("建档后分类为空", new_part.category_id is None)
    check("新器件会出现在需补货列表",
          any(r.id == new_pid for r in svc.search.low_stock()))

    # ---------------------------------------------------------------- 迁移
    print("\n迁移 · 老库（part.manufacturer）升到 v2")
    # 造一个"老版本"的库：part 上还带 manufacturer，版本号记 1。
    # 升级必须把原有厂商信息**搬到批次上**，不能丢。
    old_path = tmpdir / "old_v1.db"
    old_db = Database(str(old_path))
    old_db.initialize()
    with old_db.transaction() as conn:
        conn.execute("ALTER TABLE part ADD COLUMN manufacturer TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "INSERT INTO part(name, mpn, manufacturer, footprint) "
            "VALUES ('10kΩ 0603 电阻', 'RC0603FR-0710KL', 'YAGEO', '0603')"
        )
        conn.execute(
            "INSERT INTO stock_lot(part_id, quantity, supplier) VALUES (1, 100, '立创商城')"
        )
        conn.execute("DROP VIEW IF EXISTS v_part_overview")
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")

    check("老库起点版本为 1", get_version(old_db) == 1)
    run_migrations(old_db)
    check("迁移后版本升到 2", get_version(old_db) == 2, str(get_version(old_db)))

    part_cols = {r["name"] for r in old_db.query("PRAGMA table_info(part)")}
    check("part 表不再有 manufacturer 列", "manufacturer" not in part_cols, str(part_cols))
    lot_cols = {r["name"] for r in old_db.query("PRAGMA table_info(stock_lot)")}
    check("stock_lot 表新增 manufacturer 列", "manufacturer" in lot_cols)

    moved = old_db.query_one("SELECT manufacturer, supplier FROM stock_lot WHERE id = 1")
    check("原有厂商信息搬到了批次上（没丢）", moved["manufacturer"] == "YAGEO",
          str(dict(moved)))
    check("批次上的供应商也没动", moved["supplier"] == "立创商城")

    view_row = old_db.query_one("SELECT id, manufacturers, suppliers FROM v_part_overview")
    check("迁移后视图可用且聚合出厂商", view_row is not None
          and view_row["manufacturers"] == "YAGEO", str(dict(view_row)) if view_row else "无")
    old_db.close()

    # 迁移必须是幂等的：再跑一次不能报错、不能重复搬
    old_db2 = Database(str(old_path))
    run_migrations(old_db2)
    again = old_db2.query_one("SELECT manufacturer FROM stock_lot WHERE id = 1")
    check("重复迁移幂等", again["manufacturer"] == "YAGEO", str(dict(again)))
    old_db2.close()

    # ---------------------------------------------------------------- 器件清单 导出 / 导入
    print("\n器件清单 · 导出 CSV 与读回")
    from app.domain.part_io import (                              # noqa: E402
        collect_export_rows,
        execute_import,
        plan_import,
        read_import_rows,
        write_export_csv,
    )

    io_cat = {c.name: c.id for c in svc.tree.list_categories()}
    io_loc = {lg.name: lg for lg in svc.tree.list_locations()}
    io_part = svc.parts.create(
        Part(name="测试导出用电阻 22kΩ", category_id=io_cat["电阻"],
             mpn="RC0603JR-0722KL", footprint="0603", keywords="22k"),
    )
    svc.stock.add_lot(io_part, 80, io_loc["元件柜A"].id, unit_price=0.0075,
                      purchase_date="2026-05-01", manufacturer="YAGEO", supplier="立创")

    io_rows = collect_export_rows(svc)
    mine = [r for r in io_rows if r.name == "测试导出用电阻 22kΩ"]
    check("导出里能找到新造的器件", len(mine) == 1, str(len(mine)))
    check("导出保留了批次的全部字段",
          mine[0].qty == 80 and abs(mine[0].unit_price - 0.0075) < 1e-12
          and mine[0].location == "元件柜A" and mine[0].manufacturer == "YAGEO",
          str(mine[0]))
    check("单价文本是 0.0075（不是科学计数法）",
          mine[0].as_cells()[7] == "0.0075", mine[0].as_cells()[7])
    check("导出带完整分类路径", mine[0].category == "电阻", mine[0].category)

    io_csv = tmpdir / "导出清单.csv"
    write_export_csv(io_csv, io_rows)
    check("导出文件已写出", io_csv.exists() and io_csv.stat().st_size > 0)

    back_rows, back_errors = read_import_rows(io_csv)
    check("读回无错误", not back_errors, str(back_errors))
    check("读回行数与导出一致", len(back_rows) == len(io_rows),
          f"{len(back_rows)} vs {len(io_rows)}")

    io_plan = plan_import(svc, back_rows)
    check("预演：库里都有 → 0 个新建", io_plan.new_parts == 0, str(io_plan.new_parts))
    check("预演：库存颗数 = 全库在库总量",
          io_plan.stock_qty == sum(r.qty for r in io_rows), str(io_plan.stock_qty))
    check("预演：不需要新建分类 / 位置（导出路径都能找回来）",
          not io_plan.new_categories and not io_plan.new_locations,
          f"{io_plan.new_categories} / {io_plan.new_locations}")

    before_io = svc.stock.total_quantity(io_part)
    io_report = execute_import(svc, back_rows)
    check("重复导入走批次合并（数量累加 80）",
          svc.stock.total_quantity(io_part) == before_io + 80,
          f"{before_io} -> {svc.stock.total_quantity(io_part)}")
    check("重复导入不改档案（型号保持原样）",
          svc.parts.get(io_part).mpn == "RC0603JR-0722KL")
    check("执行报告无错误", not io_report.errors, str(io_report.errors))

    print("\n器件清单 · 导入（新建 + 自动建分类 / 位置）")
    new_csv = tmpdir / "导入清单.csv"
    new_csv.write_text(
        "器件名称,型号,封装,分类,数量,位置,单价,购买日期,厂商\n"
        "测试导入用 LDO,LDO-3.3,SOT-223,芯片/IC / 电源芯片,10,新柜子 / 第三层,0.45,2026-04-01,AMS\n"
        ",NO-NAME,0603,电阻,3,,,\n",
        encoding="utf-8-sig",
    )
    new_rows, new_errs = read_import_rows(new_csv)
    check("无名称的行被跳过并记错误", len(new_rows) == 1 and len(new_errs) == 1,
          f"{len(new_rows)} / {new_errs}")

    np = plan_import(svc, new_rows)
    check("预演：1 个新建", np.new_parts == 1, str(np.new_parts))
    check("预演：自动建分类「芯片/IC / 电源芯片」（父名带斜杠不上当）",
          np.new_categories == ["芯片/IC / 电源芯片"], str(np.new_categories))
    check("预演：自动建位置 2 个（逐级）",
          np.new_locations == ["新柜子", "新柜子 / 第三层"], str(np.new_locations))

    nr = execute_import(svc, new_rows)
    check("执行：建档 1 个 / 入库 1 行 10 颗",
          (nr.created_parts, nr.lots_in, nr.qty_in) == (1, 1, 10),
          f"{nr.created_parts} / {nr.lots_in} / {nr.qty_in}")
    ldo = svc.db.query_one("SELECT id, category_id FROM part WHERE name = '测试导入用 LDO'")
    check("LDO 已建档", ldo is not None)
    check("LDO 挂在「电源芯片」下（带斜杠的父分类没被拆错）",
          svc.tree.category_path(ldo["category_id"]) == "芯片/IC / 电源芯片",
          svc.tree.category_path(ldo["category_id"]))
    ldo_lot = svc.db.query_one(
        "SELECT location_id FROM stock_lot WHERE part_id = ? AND quantity > 0", (ldo["id"],))
    check("LDO 的库存位置是「新柜子 / 第三层」",
          svc.tree.location_path(ldo_lot["location_id"]) == "新柜子 / 第三层",
          svc.tree.location_path(ldo_lot["location_id"]))

    print("\n器件清单 · 导入的坏行与开关")
    bad_csv = tmpdir / "坏行清单.csv"
    bad_csv.write_text(
        "器件名称,数量,单价\n"
        "坏数量的,abc,1.2\n"
        "坏单价的,3,xyz\n"
        "负数量的,-5,1\n"
        "正常的,4,0.5\n",
        encoding="utf-8",
    )
    bad_rows, bad_errs = read_import_rows(bad_csv)
    check("坏行被拦下，只剩 1 行有效", len(bad_rows) == 1 and len(bad_errs) == 3,
          f"{len(bad_rows)} 行 / {len(bad_errs)} 条错误")
    check("错误说明带行号", all(bad_errs[i].startswith(f"第 {i + 2} 行") for i in range(3)),
          str(bad_errs))

    switch_csv = tmpdir / "开关清单.csv"
    switch_csv.write_text("器件名称,数量\n测试开关器件,7\n", encoding="utf-8")
    sw_rows, _ = read_import_rows(switch_csv)
    sw_report = execute_import(svc, sw_rows, with_stock=False)
    check("开关关闭：只建档不入库",
          (sw_report.created_parts, sw_report.lots_in, sw_report.skipped_stock_lines) == (1, 0, 1),
          f"{sw_report.created_parts} / {sw_report.lots_in} / {sw_report.skipped_stock_lines}")
    sw_qty = svc.db.query_one(
        "SELECT total_qty FROM v_part_overview WHERE name = '测试开关器件'")["total_qty"]
    check("开关关闭：库存保持 0", sw_qty == 0, str(sw_qty))

    # ---------------------------------------------------------------- 日志
    print("\n日志：目录 / 分类 / 超量清理")
    from app import log_setup

    # log 目录重定向到临时目录，别往真实的 log/ 里写测试垃圾。
    # log_setup 内部引用的是模块属性 log_dir，patch 它即可整体生效。
    fake_log = tmpdir / "log"
    original_log_dir = log_setup.log_dir

    def fake_log_dir(create: bool = True) -> Path:
        # 桩要保留原函数"按需建目录"的行为 —— setup_logging 靠它建目录
        if create:
            fake_log.mkdir(parents=True, exist_ok=True)
        return fake_log

    log_setup.log_dir = fake_log_dir
    try:
        log_setup.setup_logging()
        logging.getLogger("edms.test").info("运行日志一条")
        logging.getLogger("edms.test").error("错误日志一条")
        for h in logging.getLogger().handlers:
            h.flush()

        info_text = (fake_log / "info.log").read_text(encoding="utf-8")
        error_text = (fake_log / "error.log").read_text(encoding="utf-8")
        check("info 文件含运行日志", "运行日志一条" in info_text)
        check("info 文件也含错误（完整时间线，用户拍板）",
              "错误日志一条" in info_text)
        check("error 文件只含错误（不被运行日志污染）",
              "错误日志一条" in error_text and "运行日志一条" not in error_text)

        # 超量清理：造 12 个假的旧日志，断言删到剩 10 且删的是最旧的
        for day in range(1, 13):
            f = fake_log / f"info.log.2026-01-{day:02d}"
            f.write_text(f"旧的 {day}", encoding="utf-8")
            stamp = 1_000_000_000 + day * 1000        # mtime 递增，1 号最旧
            os.utime(f, (stamp, stamp))
        log_setup._prune_old_logs()
        left = sorted(fake_log.glob("info.log.*"))
        check("超量清理后只剩 10 个", len(left) == 10, f"剩 {len(left)} 个")
        check("删的是最旧的（1、2 号没了，12 号还在）",
              not (fake_log / "info.log.2026-01-01").exists()
              and not (fake_log / "info.log.2026-01-02").exists()
              and (fake_log / "info.log.2026-01-12").exists())
    finally:
        # 摘掉测试装的 handler：一是别让后续输出混进临时文件，
        # 二是 Windows 上句柄不关，atexit 删临时目录会因文件占用失败
        log_setup.log_dir = original_log_dir
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()

    svc.close()
    print(f"\n{'=' * 62}\n全部通过：{PASSED} 项断言\n{'=' * 62}")


if __name__ == "__main__":
    main()
