"""界面逻辑测试（无头运行，不弹任何窗口）。

    .venv/Scripts/python.exe .workbuddy/tools/ui_test.py

它验证的是"控件状态和数据显示对不对"，而不是"好不好看"——
后者只能靠截图核对，见 .workbuddy/shot_*.png。

具体覆盖：
  · 快速查询浮窗：输入 -> 防抖 -> 结果条数 / 首行选中 / 出库后再刷新
  · 主窗口：模型行数、搜索筛选、低库存筛选、详情面板内容、分类树筛选
  · 数值格式化与状态判定（缺货 / 低库存 / 正常）

关键点是 pump()：Qt 的 QTimer（防抖）不会自己跑，必须真的转一会儿事件循环，
直接 sleep 是测不出来的——这正是模拟按键截图容易得出错误结论的原因。
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QEventLoop, Qt, QTimer              # noqa: E402
from PySide6.QtGui import QColor                               # noqa: E402
from PySide6.QtWidgets import (                                  # noqa: E402
    QApplication,
    QDialogButtonBox,
    QMessageBox,
    QPushButton,
)


def _iter_top(dialog):
    """树管理对话框里的顶层节点列表。"""
    return [dialog.tree.topLevelItem(i)
            for i in range(dialog.tree.topLevelItemCount())]


def _select_by_id(dialog, node_id):
    """按 id 选中树里的节点。

    `_reload()` 会 `tree.clear()`，选中随之丢失 —— 所以每次操作前都得重选，
    这不是测试偷懒，是界面真实的行为。
    """
    def walk(item):
        yield item
        for i in range(item.childCount()):
            yield from walk(item.child(i))

    for top in _iter_top(dialog):
        for node in walk(top):
            if node.data(0, int(Qt.UserRole) + 3) == node_id:
                dialog.tree.setCurrentItem(node)
                return node
    raise AssertionError(f"树上找不到节点 {node_id}")

from app.domain import Services                                  # noqa: E402
from app.domain.models import Part, SearchQuery                  # noqa: E402
from app.ui.bom_dialog import BomDialog                          # noqa: E402
from app.ui.main_window import MainWindow                        # noqa: E402
from app.ui.quick_search import QuickSearchWindow                # noqa: E402
from app.ui.stock_dialog import StockInDialog, StockOutDialog    # noqa: E402
from app.ui import theme                                         # noqa: E402

ROLE_ID = int(Qt.UserRole) + 1

# 无头测试里不能让模态对话框弹出来卡住，统一替换掉。
# 只替换测试进程内的绑定，不影响真实运行。
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.critical = staticmethod(lambda *a, **k: None)

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        print(f"  [FAIL] {label}  {detail}")
        raise SystemExit(1)
    PASSED += 1
    print(f"  [ ok ] {label}{('  -> ' + detail) if detail else ''}")


def pump(ms: int = 400) -> None:
    """真的转一会儿事件循环。QTimer 只会在事件循环里触发。"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def build_data(svc: Services) -> None:
    """建一批可预期的测试数据。"""
    cats = {c.name: c.id for c in svc.tree.list_categories()}
    locs = {l.code: l.id for l in svc.tree.list_locations() if l.code}

    resistor = cats["电阻"]
    tpl = {t.name: t.id for t in svc.parts.templates_for_category(resistor)}

    def add(name, mpn, footprint, qty, price, min_stock, code, params=None):
        pid = svc.parts.create(
            Part(name=name, category_id=resistor, mpn=mpn, footprint=footprint,
                 keywords="res 电阻", min_stock=min_stock),
            params=params or {},
        )
        if qty > 0:
            svc.stock.add_lot(pid, qty, locs.get(code), unit_price=price,
                              purchase_date="2026-03-15", supplier="测试")
        return pid

    add("10kΩ 0603 1% 电阻", "RC0603FR-0710KL", "0603", 700, 0.0086, 100, "A1",
        {tpl["阻值"]: 10000.0, tpl["精度"]: 1.0, tpl["功率"]: 0.1})
    add("100Ω 0603 1% 电阻", "RC0603FR-07100RL", "0603", 12, 0.008, 50, "A1",
        {tpl["阻值"]: 100.0, tpl["精度"]: 1.0})
    add("1kΩ 0805 1% 电阻", "RC0805FR-071KL", "0805", 0, 0.0, 50, "A1",
        {tpl["阻值"]: 1000.0})

    cap = cats["电容"]
    cap_tpl = {t.name: t.id for t in svc.parts.templates_for_category(cap)}
    svc.parts.create(
        Part(name="100nF 0603 50V X7R 电容", category_id=cap, mpn="CL10B104KB8NNNC",
             footprint="0603", keywords="cap 电容", min_stock=200),
        params={cap_tpl["容值"]: 100.0, cap_tpl["耐压"]: 50.0, cap_tpl["介质"]: "X7R"},
    )


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="edms_uitest_"))
    app = QApplication(sys.argv)
    theme.apply(app)

    # 别让测试改写真实的 data/settings.json —— 载入 BOM、导入清单都会写 bom_dir，
    # 所以这层隔离要从第一个测试段之前就生效（以前放在中间，前面的段会漏出去）。
    import app.config as app_config
    original_cfg = app_config.CONFIG_PATH
    app_config.CONFIG_PATH = tmpdir / "settings.json"

    svc = Services.bootstrap(str(tmpdir / "test.db"))
    build_data(svc)

    # 把临时库登记到退出时清理（atexit 是 LIFO，所以先关连接再删目录）
    atexit.register(shutil.rmtree, tmpdir, ignore_errors=True)
    atexit.register(svc.close)

    # ================================================================
    print("快速查询浮窗")
    quick = QuickSearchWindow(svc)
    quick.popup()
    pump(300)

    check("空关键词列出全部器件", quick.tree.topLevelItemCount() == 4,
          f"{quick.tree.topLevelItemCount()} 行")

    # ---- 关键词筛选：断言"界面显示的 == 服务返回的"，
    #      而不是把条数写死——写死的话，数据一改测试就假失败 ----
    def expect_rows(keyword: str) -> int:
        return len(svc.search.search(SearchQuery(keyword=keyword, limit=20)))

    quick.ed_input.setText("10k")
    pump(400)   # 必须真的等防抖跑完，sleep 是没用的
    want = expect_rows("10k")
    check(f"输入 10k 后行数与检索一致（{want} 条）",
          quick.tree.topLevelItemCount() == want,
          f"界面 {quick.tree.topLevelItemCount()} 行")
    names = [quick.tree.topLevelItem(i).text(0)
             for i in range(quick.tree.topLevelItemCount())]
    check("10kΩ 出现在结果里", any("10kΩ" in n for n in names), str(names))
    check("首行已自动选中", quick.tree.currentItem() is not None)
    check("计数标签与行数一致",
          quick.lbl_count.text().startswith(str(want)), quick.lbl_count.text())

    quick.ed_input.setText("10")
    pump(400)
    want10 = expect_rows("10")
    check(f"输入 10 后行数一致（{want10} 条）",
          quick.tree.topLevelItemCount() == want10,
          f"界面 {quick.tree.topLevelItemCount()} 行")

    quick.ed_input.setText("zzz不存在")
    pump(400)
    check("无匹配时列表清空", quick.tree.topLevelItemCount() == 0)
    check("无匹配有提示", "无匹配" in quick.lbl_count.text(), quick.lbl_count.text())

    # 防抖必须真的生效：连续改三次文本，只应得到最后一次的结果
    quick.ed_input.setText("1")
    quick.ed_input.setText("1k")
    quick.ed_input.setText("10k")
    pump(400)
    check("防抖后只呈现最后一次输入的结果",
          quick.tree.topLevelItemCount() == want,
          f"界面 {quick.tree.topLevelItemCount()} 行")

    # ---- 快速出库 ----
    quick.ed_input.setText("10k")
    pump(400)
    before = svc.stock.total_quantity(
        svc.db.query_one("SELECT id FROM part WHERE name LIKE '10k%'")["id"]
    )
    quick._quick_withdraw()
    pump(300)
    after = svc.db.query_one(
        "SELECT COALESCE(SUM(quantity),0) AS n FROM stock_lot WHERE part_id = "
        "(SELECT id FROM part WHERE name LIKE '10k%')"
    )["n"]
    check("快速出库扣减 1 个", after == before - 1, f"{before} -> {after}")
    check("快速出库写入流水",
          svc.db.query_one(
              "SELECT COUNT(*) AS n FROM stock_log WHERE ref='热键快速出库'")["n"] == 1)

    # ================================================================
    print("\n主窗口")
    win = MainWindow(svc)

    check("模型行数等于器件总数", win.model.rowCount() == 4, f"{win.model.rowCount()} 行")
    check("分类树根节点存在", win.tree.topLevelItem(0).text(0) == "全部器件")

    # ---- 关键词筛选 ----
    win.ed_search.setText("10k")
    win._reload_table()
    check("搜索 10k 后剩 1 行", win.model.rowCount() == 1, f"{win.model.rowCount()} 行")

    win.ed_search.setText("0603")
    win._reload_table()
    check("搜索 0603 后 3 行", win.model.rowCount() == 3, f"{win.model.rowCount()} 行")

    win.ed_search.clear()
    win._reload_table()
    check("清空搜索后恢复 4 行", win.model.rowCount() == 4, f"{win.model.rowCount()} 行")

    # ---- 低库存筛选（同样对照服务，不写死条数） ----
    want_low = len(svc.search.low_stock())
    win.chk_low.setChecked(True)
    win._reload_table()
    low_rows = win.model.rowCount()
    check(f"只看需补货 -> {want_low} 行", low_rows == want_low, f"{low_rows} 行")
    states = {win.model.row_at(i).stock_state for i in range(low_rows)}
    check("告警行状态都落在 out/low", states <= {"out", "low"}, str(states))
    check("缺货与低库存两种状态都出现", states == {"out", "low"}, str(states))

    win.chk_low.setChecked(False)
    win._reload_table()

    # ---- 分类树筛选 ----
    def find_item(parent, cat_id):
        for i in range(parent.childCount()):
            c = parent.child(i)
            if c.data(0, ROLE_ID) == cat_id:
                return c
            r = find_item(c, cat_id)
            if r:
                return r
        return None

    cap_id = next(c.id for c in svc.tree.list_categories() if c.name == "电容")
    node = find_item(win.tree.topLevelItem(0), cap_id)
    check("能找到电容分类节点", node is not None)
    win.tree.setCurrentItem(node)
    win._reload_table()
    check("选中电容后只剩 1 行", win.model.rowCount() == 1, f"{win.model.rowCount()} 行")

    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    win._reload_table()

    # ---- 详情面板 ----
    pid_10k = svc.db.query_one("SELECT id FROM part WHERE name LIKE '10k%'")["id"]
    win.table.selectRow(
        next(i for i in range(win.model.rowCount()) if win.model.row_at(i).id == pid_10k)
    )
    pump(200)
    check("详情标题正确", "10kΩ" in win.lbl_title.text(), win.lbl_title.text())
    check("详情显示总库存", "总库存" in win.lbl_stock.text(), win.lbl_stock.text())
    check("规格参数已填充", win.tbl_params.rowCount() == 3, f"{win.tbl_params.rowCount()} 行")
    check("参数带单位显示",
          win.tbl_params.item(0, 1).text() == "10000 Ω",
          win.tbl_params.item(0, 1).text())
    check("批次表已填充", win.tbl_lots.rowCount() == 1, f"{win.tbl_lots.rowCount()} 行")
    check("批次位置显示名字（用户要求位置不要编号）",
          win.tbl_lots.item(0, 0).text() == "A1-电阻",
          win.tbl_lots.item(0, 0).text())
    check("流水区有内容", "出库" in win.txt_logs.toPlainText() or "入库" in win.txt_logs.toPlainText(),
          win.txt_logs.toPlainText()[:60])

    # ---- 缺货器件详情 ----
    pid_out = svc.db.query_one("SELECT id FROM part WHERE name LIKE '1kΩ%'")["id"]
    win.table.selectRow(
        next(i for i in range(win.model.rowCount()) if win.model.row_at(i).id == pid_out)
    )
    pump(200)
    check("缺货器件状态标注为已缺货", "已缺货" in win.lbl_state.text(), win.lbl_state.text())

    # ---- 表格显示文本 ----
    row_out = next(i for i in range(win.model.rowCount()) if win.model.row_at(i).id == pid_out)
    idx = win.model.index(row_out, 0)
    check("缺货行名称带 ⊘ 前缀", win.model.data(idx).startswith("⊘ "), win.model.data(idx))
    idx_qty = win.model.index(row_out, 2)
    check("缺货行库存显示 0", win.model.data(idx_qty) == "0", win.model.data(idx_qty))

    total = win.status_label.text()
    check("状态栏统计了器件种类", "共 4 种器件" in total, total)

    # ================================================================
    print("\nBOM 匹配对话框")
    bom_csv = tmpdir / "ui_bom.csv"
    bom_csv.write_text(
        "No.,Quantity,Comment,Designator,Footprint,Value,"
        "Manufacturer Part,Manufacturer,Supplier Part,Supplier\n"
        "1,2,10k,R1 R2,R0603,10k,,,,\n"
        "2,1,100nF,C1,C0603,100nF,,,,\n"
        "3,5,库里没有的料,X1,XXX-1,,,,\n",
        encoding="utf-8-sig",
    )

    dlg = BomDialog(svc)
    dlg.load(bom_csv)
    pump(200)

    check("对话框行数 = BOM 行数", dlg.table.rowCount() == 3, f"{dlg.table.rowCount()} 行")
    check("高分行默认勾选", dlg.table.item(0, 0).checkState() == Qt.Checked)
    check("库存不足的行默认不勾选", dlg.table.item(1, 0).checkState() == Qt.Unchecked)
    check("未匹配的行默认不勾选", dlg.table.item(2, 0).checkState() == Qt.Unchecked)

    top0 = dlg._matches[0].candidates[0]
    check("10k 匹配到正确器件", "10kΩ" in top0.part_name, top0.part_name)
    check("匹配依据写明了参数与封装",
          any("参数一致" in x for x in top0.evidence)
          and any("封装一致" in x for x in top0.evidence),
          str(top0.evidence))
    check("完全匹配标记为 True", dlg._matches[0].exact is True)

    check("未匹配行没有候选", dlg._matches[2].candidates == [])
    check("库存列显示需求量",
          "需 2" in dlg.table.item(0, 7).text(), dlg.table.item(0, 7).text())
    check("未指定器件时库存列显示 —", dlg.table.item(2, 7).text() == "—")

    # 状态列三态：# 对应 bom_dialog._STATE_ROLE = Qt.UserRole + 10
    STATE_ROLE = int(Qt.UserRole) + 10
    check("完全匹配行状态为 ok",
          dlg.table.item(0, 5).data(STATE_ROLE) == "ok",
          str(dlg.table.item(0, 5).data(STATE_ROLE)))
    check("缺料行状态为 missing",
          dlg.table.item(2, 5).data(STATE_ROLE) == "missing")
    check("状态列画出了图标", not dlg.table.item(0, 5).icon().isNull())
    check("状态列不再显示分数",
          dlg.table.item(0, 5).text() == "", repr(dlg.table.item(0, 5).text()))
    headers = [dlg.table.horizontalHeaderItem(i).text()
               for i in range(dlg.table.columnCount())]
    # 精确比对表头名 —— 用 in 会误判，因为器件列的表头是
    # "匹配到的器件（按匹配度排序）"，里面本来就有"匹配度"三个字
    check("表头里没有独立的「匹配度」列", "匹配度" not in headers, str(headers))
    check("表头里没有「匹配依据」列", "匹配依据" not in headers, str(headers))
    check("有「状态」列", "状态" in headers, str(headers))

    # 改选：把第 2 行手动指到 100nF 那个器件上，模型要跟着更新
    combo = dlg.table.cellWidget(1, 6)
    target_id = svc.db.query_one("SELECT id FROM part WHERE name LIKE '100nF%'")["id"]
    combo.setCurrentIndex(combo.findData(target_id))
    pump(150)
    check("改选后模型同步更新", dlg._matches[1].part_id == target_id)
    check("改选后库存仍不足 → 不 ready", dlg._matches[1].ready is False)
    check("改选后状态列不再是 missing",
          dlg.table.item(1, 5).data(STATE_ROLE) != "missing")

    # 点状态图标看原因：不能抛异常（模态框已在文件头被替换成空实现）
    dlg.show_reason(0)
    dlg.show_reason(2)
    check("点击状态图标查看原因不报错", True)

    # 一键全出库：只处理勾选且可就绪的行
    pid_10k = svc.db.query_one("SELECT id FROM part WHERE name LIKE '10k%'")["id"]
    before_total = svc.stock.total_quantity(pid_10k)
    dlg.run_withdraw()
    pump(200)
    after_total = svc.stock.total_quantity(pid_10k)
    check("一键出库扣掉了 BOM 需求量", after_total == before_total - 2,
          f"{before_total} -> {after_total}")
    check("出库流水带上了 BOM 文件名与行号",
          svc.db.query_one(
              "SELECT COUNT(*) AS n FROM stock_log WHERE ref LIKE 'ui_bom.csv #%'"
          )["n"] >= 1)

    # 全有才出：故意把一行改成库存不足，整批应当取消
    before_all = {pid: svc.stock.total_quantity(pid) for pid in (pid_10k, target_id)}
    dlg.table.item(1, 0).setCheckState(Qt.Checked)      # 100nF 库存为 0
    dlg.run_withdraw()
    pump(200)
    after_all = {pid: svc.stock.total_quantity(pid) for pid in (pid_10k, target_id)}
    check("有行库存不足时整批取消（10k 没被扣）",
          after_all[pid_10k] == before_all[pid_10k],
          f"{before_all[pid_10k]} -> {after_all[pid_10k]}")

    dlg.close()

    # ================================================================
    print("\nBOM · 总钱数与成本明细")
    from app.ui.bom_dialog import BomCostDialog

    # 一个"入库时忘了填单价"的器件：有货、有批次、但单价是 0
    cap_id = {c.name: c.id for c in svc.tree.list_categories()}["电容"]
    cap_tpl = {t.name: t.id for t in svc.parts.templates_for_category(cap_id)}
    part_np = svc.parts.create(
        Part(name="1nF 0603 50V 电容", category_id=cap_id, mpn="CL10B101KB8NNNC",
             footprint="0603"),
        params={cap_tpl["容值"]: 1.0},
    )
    svc.stock.add_lot(part_np, 30, None, unit_price=0.0, purchase_date="2026-01-01")

    cost_csv = tmpdir / "test_bom_cost.csv"
    cost_csv.write_text(
        "No.,Quantity,Comment,Designator,Footprint,Manufacturer Part\n"
        "1,4,1nF 0603 50V 电容,C1,C0603,CL10B101KB8NNNC\n"
        "2,2,10kΩ 0603 1% 电阻,R1,R0603,RC0603FR-0710KL\n"
        "3,1,库里没有的料,U9,XXX-1,\n",
        encoding="utf-8-sig",
    )

    dlg_cost = BomDialog(svc)
    dlg_cost.load(cost_csv)
    pump(200)

    # 右下角的总价按钮
    check("载入后总价按钮可用", dlg_cost.btn_cost.isEnabled())
    check("总价按钮带金额",
          dlg_cost.btn_cost.text().startswith("总价 ¥"),
          dlg_cost.btn_cost.text())
    check("总价文本列出待补价的项数", "待补价" in dlg_cost.btn_cost.text(),
          dlg_cost.btn_cost.text())
    check("存在算不出来的部分 → 颜色是黄（attention）",
          dlg_cost.btn_cost.styleSheet() == f"color: {theme.pal().fg_attention};",
          dlg_cost.btn_cost.styleSheet())
    check("没载入 BOM 时总价按钮是灰的", (
        (lambda d: (d.btn_cost.isEnabled() is False))(BomDialog(svc))))

    # 点总价 → 成本明细
    cost_dlg = BomCostDialog(svc, dlg_cost._cost, dlg_cost)
    pump(150)
    check("缺价的行置顶（第一行就是没价格的）",
          cost_dlg._lines[0].part_id == part_np, cost_dlg._lines[0].label)
    check("没匹配上的行排在缺价行后面",
          cost_dlg._lines[1].matched is False)
    check("有价格的行排最后",
          cost_dlg._lines[2].unit_price > 0)

    pal = theme.pal()
    check("缺价行整行黄底",
          cost_dlg.table.item(0, 1).background().color().name().lower()
          == QColor(pal.bg_low).name().lower(),
          cost_dlg.table.item(0, 1).background().color().name())
    check("未匹配行红底",
          cost_dlg.table.item(1, 1).background().color().name().lower()
          == QColor(pal.bg_out).name().lower(),
          cost_dlg.table.item(1, 1).background().color().name())
    check("正常行不着色",
          cost_dlg.table.item(2, 1).background().color().name().lower()
          in ("#000000", "transparent"),
          cost_dlg.table.item(2, 1).background().color().name())

    # 单价列只有缺价行可编辑（靠 ItemIsEditable，不是塞控件）
    check("缺价行的单价可编辑",
          bool(cost_dlg.table.item(0, 4).flags() & Qt.ItemIsEditable))
    check("没填价显示成 — 而不是 0（0 会被读成「不要钱」）",
          cost_dlg.table.item(0, 4).text() == "—",
          cost_dlg.table.item(0, 4).text())
    check("未匹配行的单价不可编辑",
          not bool(cost_dlg.table.item(1, 4).flags() & Qt.ItemIsEditable))

    # 就地补价 → 立即写库（价格记在批次上）
    check("改价前还没标记为已改动", cost_dlg.changed is False)
    cost_dlg.table.item(0, 4).setText("0.032")
    pump(200)
    lot_price = svc.db.query_one(
        "SELECT unit_price FROM stock_lot WHERE part_id = ? AND quantity > 0",
        (part_np,))["unit_price"]
    check("改价落库（写到在库批次）", abs(lot_price - 0.032) < 1e-9, repr(lot_price))
    check("改价后标记为已改动", cost_dlg.changed is True)
    check("改价后那行不再算「缺价」",
          all(l.part_id != part_np for l in cost_dlg.report.missing))
    check("改价后那行小计跟着变",
          cost_dlg.table.item(0, 5).text() == "0.128",
          cost_dlg.table.item(0, 5).text())
    check("合计标签不再显示「还没价格」", "还没价格" not in cost_dlg.lbl_total.text(),
          cost_dlg.lbl_total.text())

    # 坏输入不能写库
    lots_before_bad = svc.db.query_one(
        "SELECT unit_price FROM stock_lot WHERE part_id = ? AND quantity > 0",
        (part_np,))["unit_price"]
    cost_dlg.table.item(0, 4).setText("abc")
    pump(200)
    check("价格填成文字会被拦下（单元格恢复原值）",
          cost_dlg.table.item(0, 4).text() == "0.032",
          cost_dlg.table.item(0, 4).text())
    check("坏输入不会改库",
          svc.db.query_one(
              "SELECT unit_price FROM stock_lot WHERE part_id = ? AND quantity > 0",
              (part_np,))["unit_price"] == lots_before_bad)

    # 关掉明细后，主界面要重算总价 —— 这回应该全绿了
    cost_dlg.close()
    # show_cost 里的明细是模态 exec，测试里换成"立即返回"。
    # 注意 show_cost 用的是**它自己 new 出来**的那个明细实例，
    # 所这里直接把它的 changed 置真，模拟"用户在里面真的改过一次价"。
    import app.ui.bom_dialog as bom_mod
    real_exec = bom_mod.BomCostDialog.exec

    def fake_exec(self) -> int:
        self.changed = True
        return 1

    bom_mod.BomCostDialog.exec = fake_exec
    try:
        dlg_cost.show_cost()
        pump(300)
    finally:
        bom_mod.BomCostDialog.exec = real_exec
    check("补完价后不再有「待补价」的行", len(dlg_cost._cost.missing) == 0)
    check("补完价后总价按钮不再提「待补价」",
          "待补价" not in dlg_cost.btn_cost.text(), dlg_cost.btn_cost.text())
    # 那份 BOM 还有一行故意匹配不上，所以总价**仍然**是黄 —— 缺料也是算不齐
    check("仍有未匹配行 → 总价保持黄（缺料同样算不齐）",
          dlg_cost.btn_cost.styleSheet() == f"color: {theme.pal().fg_attention};",
          dlg_cost.btn_cost.styleSheet())
    cost_dlg2 = BomCostDialog(svc, dlg_cost._cost, dlg_cost)
    check("重算后明细里没有待补价行", not cost_dlg2.report.missing)
    cost_dlg2.close()

    dlg_cost.close()

    # ================================================================
    print("\nBOM 操作 · 统一入口的三页签")
    dlg_tabs = BomDialog(svc)
    pump(150)
    check("对话框有 3 个页签", dlg_tabs.tabs.count() == 3, str(dlg_tabs.tabs.count()))
    check("页签顺序是 比对 / 导出 / 导入",
          [dlg_tabs.tabs.tabText(i) for i in range(3)] == ["比对", "导出", "导入"],
          str([dlg_tabs.tabs.tabText(i) for i in range(3)]))
    check("比对页的原控件都还在（加载 / 总价 / 出库）",
          dlg_tabs.table is not None and dlg_tabs.btn_cost is not None
          and dlg_tabs.btn_withdraw is not None)
    check("对话框底部有共用的关闭按钮",
          any(b.text() == "关闭" for b in dlg_tabs.findChildren(QPushButton)))

    # ---- 导出页 ----
    export_path = tmpdir / "ui_导出清单.csv"
    dlg_tabs.export_panel._ask_save_path = lambda: str(export_path)
    check("导出面板统计了当前库存",
          "个器件" in dlg_tabs.export_panel.lbl_stats.text(),
          dlg_tabs.export_panel.lbl_stats.text())
    dlg_tabs.export_panel.export()
    pump(150)
    check("导出动作写出了文件", export_path.exists() and export_path.stat().st_size > 0)

    from app.domain.part_io import read_import_rows as _read_rows
    back_rows, back_errs = _read_rows(export_path)
    check("导出的文件能直接读回（闭环）", bool(back_rows) and not back_errs,
          str(back_errs))
    back_names = {r.name for r in back_rows}
    check("读回的清单含已有的 10kΩ 器件",
          any("10kΩ" in n for n in back_names), str(sorted(back_names))[:80])

    # ---- 导入页 ----
    imp_csv = tmpdir / "ui_导入清单.csv"
    imp_csv.write_text(
        "器件名称,型号,封装,分类,数量,位置,单价,购买日期\n"
        "UI导入测试器件,CH340C,SOP-16,芯片/IC,25,元件柜B,1.8,2026-06-01\n"
        "10kΩ 0603 1% 电阻,RC0603FR-0710KL,0603,电阻,3,元件柜A,0.008,2026-06-02\n"
        ",无名行,0603,电阻,5,,,\n",       # 这行没有器件名称，会被跳过
        encoding="utf-8-sig",
    )
    dlg_tabs.import_panel.load(imp_csv)
    pump(150)
    plan_text = dlg_tabs.import_panel.lbl_plan.text()
    check("导入预览：1 个新建", "新建 1 个" in plan_text, plan_text)
    check("导入预览：2 行入库合计 28 颗", "28 颗" in plan_text, plan_text)
    check("导入预览：提示有问题的行", "另有 1 行有问题" in plan_text, plan_text)
    check("预览后「开始导入」可用", dlg_tabs.import_panel.btn_import.isEnabled())

    n_before = len(dlg_tabs._all_parts)
    dlg_tabs.import_panel.run_import()      # 确认框在文件头被替换成「Yes」
    pump(250)
    report_text = dlg_tabs.import_panel.txt_report.toPlainText()
    check("导入后报告区有结果明细",
          "导入结果" in report_text and "新建 1 个" in report_text,
          report_text.replace("\n", " / ")[:120])
    check("报告里列出被跳过的行（带行号）",
          "第 4 行" in report_text, report_text.replace("\n", " / ")[:150])
    check("导入后器件建档",
          svc.db.query_one("SELECT id FROM part WHERE name = 'UI导入测试器件'") is not None)
    stock_row = svc.db.query_one(
        "SELECT total_qty FROM v_part_overview WHERE name = 'UI导入测试器件'")
    check("导入后库存 25 颗", stock_row["total_qty"] == 25, str(stock_row["total_qty"]))
    check("导入后比对页器件缓存刷新（下拉框能选到新器件）",
          len(dlg_tabs._all_parts) == n_before + 1,
          f"{n_before} -> {len(dlg_tabs._all_parts)}")
    check("导入后文件被重置（防同一份文件顺手再导一遍）",
          not dlg_tabs.import_panel.btn_import.isEnabled()
          and "已导入" in dlg_tabs.import_panel.lbl_file.text())

    # 开关关闭 → 只建档不入库
    imp2 = tmpdir / "ui_导入开关.csv"
    imp2.write_text("器件名称,数量\nUI开关器件,9\n", encoding="utf-8")
    dlg_tabs.import_panel.load(imp2)
    dlg_tabs.import_panel.chk_stock.setChecked(False)
    dlg_tabs.import_panel.run_import()
    pump(200)
    sw = svc.db.query_one("SELECT total_qty FROM v_part_overview WHERE name = 'UI开关器件'")
    check("开关关闭：建档了但库存为 0", sw is not None and sw["total_qty"] == 0,
          str(dict(sw)) if sw else "None")
    check("开关关闭：报告里写明跳过入库",
          "跳过" in dlg_tabs.import_panel.txt_report.toPlainText(),
          dlg_tabs.import_panel.txt_report.toPlainText()[:100])

    dlg_tabs.close()

    # ================================================================
    print("\n入库面板 · 选器件区与位置管理入口")
    from app.ui.stock_dialog import TreeManageDialog

    probe_dlg = StockInDialog(svc)
    check("① 选器件区有「＋ 新建器件」按钮",
          any(b.text() == "＋ 新建器件" for b in probe_dlg.findChildren(QPushButton)))
    check("位置下拉框旁边有「…」设置按钮",
          any(b.text() == "…" for b in probe_dlg.cb_location.parentWidget()
              .findChildren(QPushButton)))
    check("内嵌「新建器件信息」小表单已删除（误建源头）",
          not hasattr(probe_dlg, "gb_new"))
    probe_dlg.close()

    dlg_cat = TreeManageDialog(svc, "category")
    pump(100)
    n_before = len(svc.tree.list_categories())
    dlg_cat.ed_name.setText("测试分类甲")
    dlg_cat._add(None)                     # 顶层
    check("新增顶层分类生效",
          len(svc.tree.list_categories()) == n_before + 1)
    new_cat = svc.db.query_one("SELECT id FROM category WHERE name = '测试分类甲'")
    check("新增后界面标记为已改动", dlg_cat.changed is True)

    _select_by_id(dlg_cat, new_cat["id"])
    dlg_cat.ed_name.setText("测试分类乙")
    dlg_cat._add(dlg_cat._selected_id())   # 子项
    child = svc.db.query_one(
        "SELECT id, parent_id FROM category WHERE name = '测试分类乙'")
    check("新增子分类挂在父节点下",
          child is not None and child["parent_id"] == new_cat["id"])

    _select_by_id(dlg_cat, new_cat["id"])
    dlg_cat.ed_name.setText("测试分类甲·改名")
    dlg_cat._rename()
    check("重命名生效",
          svc.db.query_one("SELECT name FROM category WHERE id = ?",
                           (new_cat["id"],))["name"] == "测试分类甲·改名")

    # 删除父分类：子分类级联删，器件不删
    _select_by_id(dlg_cat, new_cat["id"])
    dlg_cat._delete()
    check("删除后父分类没了",
          svc.db.query_one("SELECT id FROM category WHERE id = ?",
                           (new_cat["id"],)) is None)
    check("子分类级联删掉",
          svc.db.query_one("SELECT id FROM category WHERE id = ?",
                           (child["id"],)) is None)
    dlg_cat.close()

    # 位置：纯文本名字，不要短码（用户明确要求位置不要编号）
    dlg_loc = TreeManageDialog(svc, "location")
    pump(100)
    n_loc_before = len(svc.tree.list_locations())
    dlg_loc.ed_name.setText("测试柜Z")
    dlg_loc._add(None)
    new_loc = svc.db.query_one("SELECT id FROM location WHERE name = '测试柜Z'")
    check("新增位置生效", len(svc.tree.list_locations()) == n_loc_before + 1
          and new_loc is not None)
    check("位置入库后界面标记为已改动", dlg_loc.changed is True)

    # 重名要给人话报错，不是 UNIQUE constraint failed
    dlg_loc.ed_name.setText("测试柜Z")
    dlg_loc._add(None)
    check("重名新增不会堆出重复项",
          len([l for l in svc.tree.list_locations() if l.name == '测试柜Z']) == 1)

    _select_by_id(dlg_loc, new_loc["id"])
    dlg_loc.ed_name.setText("测试柜Z·改名")
    dlg_loc._rename()
    row_loc = svc.db.query_one(
        "SELECT name FROM location WHERE id = ?", (new_loc["id"],))
    check("位置重命名生效", row_loc["name"] == "测试柜Z·改名",
          str(dict(row_loc)))

    # 删掉的位置：批次不删，location_id 置空
    probe = svc.parts.create(Part(name="位置删除探针", footprint="0603"))
    svc.stock.add_lot(probe, 5, new_loc["id"], unit_price=0.01)
    _select_by_id(dlg_loc, new_loc["id"])
    dlg_loc._delete()
    check("删除位置后批次还在",
          svc.stock.total_quantity(probe) == 5)
    check("批次的 location_id 被置空",
          svc.db.query_one("SELECT location_id FROM stock_lot WHERE part_id = ?",
                           (probe,))["location_id"] is None)
    dlg_loc.close()
    dlg_loc.close()

    # ================================================================
    print("\n入库 / 出库面板")

    def ok_button(dialog):
        return dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)

    # ---- 入库：搜到就选 ----
    dlg_in = StockInDialog(svc)
    pump(200)
    check("入库面板初始没有选中器件", dlg_in.part_id is None)
    check("入库面板的「入库」按钮初始禁用", not ok_button(dlg_in).isEnabled())

    dlg_in.search.ed_search.setText("10k")
    pump(400)
    rows = dlg_in.search.list.topLevelItemCount()
    check("入库面板能搜到器件", rows >= 1, f"{rows} 项")
    check("结果列表里不再有自动兜底行（误建源头已拆）",
          all(dlg_in.search.list.topLevelItem(i).data(0, Qt.UserRole + 2) is None
              for i in range(rows)))

    # 搜不到：只给提示，不搞兜底行，更不会"回车就建"
    dlg_in_miss = StockInDialog(svc)
    dlg_in_miss.search.ed_search.setText("ZZZ库房里绝对没有的器件")
    pump(400)
    check("搜不到时列表为空", dlg_in_miss.search.list.topLevelItemCount() == 0)
    check("搜不到时提示引导点「＋ 新建器件」",
          "新建器件" in dlg_in_miss.search.lbl_hint.text(),
          dlg_in_miss.search.lbl_hint.text())
    dlg_in_miss.close()

    pid_10k = svc.db.query_one("SELECT id FROM part WHERE name LIKE '10k%'")["id"]
    dlg_in.search.picked.emit(pid_10k)
    pump(150)
    check("选中后记录了器件", dlg_in.part_id == pid_10k)
    check("选中后「入库」按钮可用", ok_button(dlg_in).isEnabled())

    before = svc.stock.total_quantity(pid_10k)
    dlg_in.sp_qty.setValue(7)
    dlg_in.ed_manufacturer.setText("YAGEO")
    dlg_in.ed_supplier.setText("立创商城")
    dlg_in._on_ok()
    pump(150)
    check("入库后库存增加", svc.stock.total_quantity(pid_10k) == before + 7,
          f"{before} -> {svc.stock.total_quantity(pid_10k)}")

    lot = svc.db.query_one(
        "SELECT manufacturer, supplier FROM stock_lot WHERE part_id = ? "
        "ORDER BY id DESC LIMIT 1", (pid_10k,))
    check("厂商记在了批次上（不是器件上）", lot["manufacturer"] == "YAGEO",
          str(dict(lot)))
    check("供应商也记在批次上", lot["supplier"] == "立创商城")
    check("part 表里已经没有厂商列",
          "manufacturer" not in {r["name"] for r in svc.db.query("PRAGMA table_info(part)")})
    check("记住了这次的厂商，下次自动带出",
          app_config.load().get("last_manufacturer") == "YAGEO")

    # ---- 入库：「＋ 新建器件」打开完整表单（和设置里同一套），建完自动选中 ----
    import app.ui.stock_dialog as sd_in_mod

    captured: dict = {}

    class _StubEditor:
        """替掉真实器件表单：无头测试不能让模态框卡住。
        模拟"用户在完整表单里建档成功"。"""

        def __init__(self, parts, labels, default_name="", services=None,
                     parent=None, **kw):
            captured["default_name"] = default_name
            captured["has_services"] = services is not None

        def exec(self) -> int:
            new_id = svc.parts.create(Part(name="47kΩ 0402 1% 电阻",
                                           footprint="0402"))
            captured["new_id"] = new_id
            self.saved_part_id = new_id
            return 1        # QDialog.Accepted

    real_editor = sd_in_mod.PartEditorDialog
    sd_in_mod.PartEditorDialog = _StubEditor
    try:
        dlg_in2 = StockInDialog(svc)
        dlg_in2.search.ed_search.setText("47kΩ 0402 1% 电阻")
        pump(300)
        dlg_in2._new_part_dialog()
        pump(150)
    finally:
        sd_in_mod.PartEditorDialog = real_editor

    check("「＋ 新建器件」把搜索框里的词带进了表单",
          captured.get("default_name") == "47kΩ 0402 1% 电阻", str(captured))
    check("表单拿到 services（分类「…」要用）", captured.get("has_services") is True)
    created_id = captured.get("new_id")
    check("建档后自动选中为新入库目标", dlg_in2.part_id == created_id,
          f"part_id={dlg_in2.part_id} / created={created_id}")
    check("建档后「入库」按钮可用", ok_button(dlg_in2).isEnabled())

    dlg_in2.sp_qty.setValue(50)
    dlg_in2._on_ok()
    pump(150)
    created = svc.db.query_one("SELECT id, footprint FROM part WHERE name = ?",
                               ("47kΩ 0402 1% 电阻",))
    check("新建的器件能正常入库并有了库存",
          created is not None and svc.stock.total_quantity(created["id"]) == 50,
          str(svc.stock.total_quantity(created["id"])) if created else "器件没建出来")
    check("新建后记住器件 id", dlg_in2.part_id == created["id"])

    # ---- 完整器件表单本体（入库按钮和设置页共用这一套） ----
    from app.ui.part_editor import PartEditorDialog, build_category_labels

    ed_dlg = PartEditorDialog(svc.parts, build_category_labels(svc.tree),
                              default_name="预填名称探针", services=svc)
    check("表单能带出默认名称（从搜索词来）",
          ed_dlg.ed_name.text() == "预填名称探针", ed_dlg.ed_name.text())
    ed_dlg.ed_name.setText("表单建档探针")
    ed_dlg.cb_footprint.setCurrentText("0805")
    ed_dlg._on_save()
    form_saved = svc.db.query_one(
        "SELECT id, footprint FROM part WHERE name='表单建档探针'")
    check("表单保存建档成功（含封装）",
          form_saved is not None and form_saved["footprint"] == "0805",
          str(dict(form_saved)) if form_saved else "没有建档")
    ed_dlg.close()

    # ================================================================
    print("\n回车守卫（EnterSafeDialog）—— 回车不再误触发「入库」")
    from PySide6.QtTest import QTest

    dlg_enter = StockInDialog(svc)
    dlg_enter.search.ed_search.setText("10k")
    pump(300)
    lots_before = svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"]

    # 搜索框回车：选中第一条（正常功能保留），但不提交
    QTest.keyClick(dlg_enter.search.ed_search, Qt.Key_Return)
    pump(150)
    check("搜索框回车 = 选中第一条，不落库",
          dlg_enter.part_id == pid_10k and dlg_enter.result() == 0
          and svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"] == lots_before,
          f"part_id={dlg_enter.part_id} result={dlg_enter.result()}")

    # 数量框回车：当年误建的场景（默认按钮被回车触发）—— 现在不落库
    QTest.keyClick(dlg_enter.sp_qty, Qt.Key_Return)
    pump(150)
    check("数量框回车不再触发「入库」",
          dlg_enter.result() == 0
          and svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"] == lots_before)
    dlg_enter.close()

    # 焦点在「入库」按钮上时回车仍要能提交（别误吞）—— 需要 show 才有焦点
    dlg_focus = StockInDialog(svc)
    dlg_focus.search.picked.emit(pid_10k)
    pump(150)
    dlg_focus.show()
    pump(100)
    okb = ok_button(dlg_focus)
    okb.setFocus()
    pump(50)
    QTest.keyClick(okb, Qt.Key_Return)
    pump(150)
    check("焦点在「入库」按钮上回车照常提交", dlg_focus.result() == 1,
          f"result={dlg_focus.result()}")
    dlg_focus.close()

    # ---- 出库：一次多个器件 ----
    pid_cap = svc.db.query_one("SELECT id FROM part WHERE name LIKE '100nF%'")["id"]
    svc.stock.add_lot(pid_cap, 100, None, unit_price=0.01, supplier="测试")

    # ---- 入库：单价可以按「元/百颗」「元/千颗」填 ----
    # 电容电阻单价小到 0.0086，照着"元/颗"填要数一串零。允许照报价单口径填，
    # 但**落库永远是单颗价** —— 均价、批次价、资产合计、BOM 都还认这一个语义。
    from app.ui import format_money
    from app.ui.stock_dialog import PRICE_UNITS

    check("价格格式化不吐科学计数法（:g 对 8.6e-05 会写成 8.6e-05）",
          format_money(0.0086) == "0.0086" and format_money(0.0) == "0",
          f"{format_money(0.0086)!r} / {format_money(0.0)!r}")
    check("报价单位是 颗 / 百颗 / 千颗 三档",
          [f for _, f in PRICE_UNITS] == [1, 100, 1000],
          str(PRICE_UNITS))

    dlg_price = StockInDialog(svc)
    pump(150)
    check("报价单位默认「元/颗」（和以前的填法一致）",
          dlg_price.cb_price_unit.currentData() == 1,
          dlg_price.cb_price_unit.currentText())
    check("价格没填时提示里就在讲计价口径",
          "元/千颗" in dlg_price.lbl_price_hint.text(),
          dlg_price.lbl_price_hint.text())

    dlg_price.search.picked.emit(pid_cap)
    pump(150)
    dlg_price.cb_price_unit.setCurrentIndex(dlg_price.cb_price_unit.findData(1000))
    dlg_price.ed_price.setText("8.6")
    dlg_price.sp_qty.setValue(5000)
    pump(150)
    hint = dlg_price.lbl_price_hint.text()
    check("换算提示给出单颗价", "0.0086" in hint, hint)
    check("换算提示给出这批合计", "43" in hint, hint)

    before_cap = svc.stock.total_quantity(pid_cap)
    dlg_price._on_ok()
    pump(150)
    lot_price = svc.db.query_one(
        "SELECT unit_price FROM stock_lot WHERE part_id = ? ORDER BY id DESC LIMIT 1",
        (pid_cap,))["unit_price"]
    check("按千颗报价换算成单颗价入库",
          abs(lot_price - 0.0086) < 1e-9, repr(lot_price))
    check("库存按填的数量增加（不是按报价单位缩水）",
          svc.stock.total_quantity(pid_cap) == before_cap + 5000,
          f"{before_cap} -> {svc.stock.total_quantity(pid_cap)}")
    check("记住了这次的报价单位",
          app_config.load().get("last_price_unit") == 1000,
          str(app_config.load().get("last_price_unit")))

    dlg_price2 = StockInDialog(svc)
    pump(150)
    check("下次打开带出上次的报价单位",
          dlg_price2.cb_price_unit.currentData() == 1000,
          dlg_price2.cb_price_unit.currentText())
    dlg_price2.search.picked.emit(pid_cap)
    pump(150)

    dlg_price2.ed_price.setText("abc")
    pump(150)
    check("价格不是数字时提示报错",
          "不是有效数字" in dlg_price2.lbl_price_hint.text(),
          dlg_price2.lbl_price_hint.text())
    lots_before = svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"]
    dlg_price2._on_ok()
    pump(150)
    check("价格非法时入库被拦住，不写库",
          svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"] == lots_before)

    dlg_price2.ed_price.setText("-1")
    pump(150)
    check("负数在提示里被点出来",
          "负数" in dlg_price2.lbl_price_hint.text(),
          dlg_price2.lbl_price_hint.text())

    lots_before_ok = svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"]
    dlg_price2.ed_price.setText("8.6")
    pump(150)
    dlg_price2._on_ok()
    pump(150)
    lots_after_ok = svc.db.query_one("SELECT COUNT(*) AS n FROM stock_lot")["n"]
    merged = svc.db.query_one(
        "SELECT unit_price, quantity FROM stock_lot WHERE part_id = ? "
        "ORDER BY id DESC LIMIT 1", (pid_cap,))
    check("同一报价换算结果稳定，能合并进上一批（精度没飘）",
          abs(merged["unit_price"] - 0.0086) < 1e-9 and lots_after_ok == lots_before_ok,
          f"单价 {merged['unit_price']!r} / 批次数 {lots_before_ok} -> {lots_after_ok}")

    # ---- 入库：价格异常提醒 ----
    # 小器件单价太小，多敲一个零就是 10 倍且不报错。提醒是"只提醒不拦"：
    # 点了继续照常入库（另起一批），存储逻辑一个字不改。
    alert_part = svc.parts.create(Part(name="异常提醒测试电阻", footprint="0603"))
    svc.stock.add_lot(alert_part, 100, None, unit_price=0.0100,
                      purchase_date="2026-01-01")

    def ask_once(dialog, price_text):
        """跑一次入库，把触发的确认框标题记下来。"""
        asked: list[str] = []
        real_question = QMessageBox.question

        def recorder(*a, **k):
            asked.append(a[1] if len(a) > 1 else "")
            return QMessageBox.Yes

        QMessageBox.question = staticmethod(recorder)
        try:
            # 报价单位会被上一次入库记住（这里前面留过「元/千颗」），
            # 不拨回「元/颗」的话填的数会被除以 1000，均价一比就成了天价
            dialog.cb_price_unit.setCurrentIndex(dialog.cb_price_unit.findData(1))
            dialog.ed_price.setText(price_text)
            pump(150)
            dialog._on_ok()
            pump(150)
        finally:
            QMessageBox.question = real_question
        return asked

    dlg_alert = StockInDialog(svc)
    dlg_alert.search.picked.emit(alert_part)
    pump(150)
    asked = ask_once(dlg_alert, "0.0105")     # 差 5%，正常波动
    check("价格差 5% 不提醒", not asked, str(asked))
    check("没提醒也正常入了库", svc.stock.total_quantity(alert_part) == 101)
    dlg_alert.close()

    dlg_alert2 = StockInDialog(svc)
    dlg_alert2.search.picked.emit(alert_part)
    pump(150)
    asked = ask_once(dlg_alert2, "0.1")       # 差 10 倍
    check("价格差 10 倍会提醒",
          len(asked) == 1 and "价格差得有点多" in asked[0], str(asked))
    check("点了继续还是照常入库（另起一批）",
          svc.stock.total_quantity(alert_part) == 102)
    check("入库后均价被拉高",
          abs(svc.stock.average_price(alert_part) - (100 * 0.01 + 0.0105 + 0.1) / 102) < 1e-9,
          f"{svc.stock.average_price(alert_part):.6f}")
    dlg_alert2.close()

    # 新器件没有均价可比，填什么价都不该提醒
    brand_new = svc.parts.create(Part(name="全新无均价器件", footprint="0603"))
    dlg_alert3 = StockInDialog(svc)
    dlg_alert3.search.picked.emit(brand_new)
    pump(150)
    asked = ask_once(dlg_alert3, "0.5")
    check("新器件没有均价，不提醒", not asked, str(asked))
    dlg_alert3.close()

    # ---- 批次右键修正：改单价 / 盘点 / 删除 ----
    # 之前服务层的 update_lot / delete_lot / adjust 一个 UI 入口都没有，
    # 批次记错了只能去数据库里动手。
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QInputDialog

    # 前面的主窗口测试留下了「只看需补货」的筛选 —— 不清掉的话，
    # 改完价 _reload_table 会把不缺货的行筛没，批次详情跟着被清空
    win._clear_filters()
    pump(150)

    win._show_detail(pid_10k)
    pump(150)
    check("详情面板登记了批次 id", len(win._lot_ids) >= 1, f"{len(win._lot_ids)} 个批次")
    lot_id = win._lot_ids[0]

    real_gettext = QInputDialog.getText
    real_getint = QInputDialog.getInt
    # 把模态菜单替掉：返回指定序号的动作（0=改单价 1=盘点 2=改日期 4=删除）
    win._exec_lot_menu = lambda menu, pos: menu.actions()[0]
    QInputDialog.getText = staticmethod(lambda *a, **k: ("0.0999", True))
    try:
        win._show_lot_menu(QPoint(5, 5))
        pump(200)
    finally:
        QInputDialog.getText = real_gettext
    check("右键改单价写进库",
          abs(svc.stock.lot(lot_id).unit_price - 0.0999) < 1e-9,
          repr(svc.stock.lot(lot_id).unit_price))
    check("改的是单价不是数量", svc.stock.lot(lot_id).quantity >= 1)

    win._exec_lot_menu = lambda menu, pos: menu.actions()[1]     # 1 = 盘点数量
    QInputDialog.getInt = staticmethod(lambda *a, **k: (7, True))
    try:
        win._show_lot_menu(QPoint(5, 5))
        pump(200)
    finally:
        QInputDialog.getInt = real_getint
    check("盘点数量直接改成实测值",
          svc.stock.lot(lot_id).quantity == 7,
          f"现在 {svc.stock.lot(lot_id).quantity}，lot_id={lot_id}，"
          f"_lot_ids={win._lot_ids}")
    check("盘点留了流水痕迹",
          svc.db.query_one(
              "SELECT COUNT(*) AS n FROM stock_log WHERE lot_id = ? AND reason = '盘点'",
              (lot_id,))["n"] >= 1)

    # 删除批次：流水保留
    part_of_lot = svc.stock.lot(lot_id).part_id
    win._exec_lot_menu = lambda menu, pos: menu.actions()[4]     # 4 = 删除
    try:
        win._show_lot_menu(QPoint(5, 5))
        pump(200)
    finally:
        pass
    check("删除后批次没了", svc.stock.lot(lot_id) is None)
    check("删除批次后流水还在（历史不该被抹掉）",
          svc.db.query_one("SELECT COUNT(*) AS n FROM stock_log WHERE part_id = ?",
                           (part_of_lot,))["n"] >= 1)
    check("批次删除后器件还在", svc.parts.get(part_of_lot) is not None)
    win._exec_lot_menu = lambda menu, pos: menu.exec(
        win.tbl_lots.viewport().mapToGlobal(pos))          # 还原成真菜单

    dlg_out = StockOutDialog(svc, preset_part_id=pid_10k)
    pump(150)
    check("出库面板带进来一个器件", dlg_out.table.rowCount() == 1)
    dlg_out.add_part(pid_cap)
    pump(150)
    check("能再加一个器件", dlg_out.table.rowCount() == 2)
    check("同一器件重复添加不会重复成两行",
          (dlg_out.add_part(pid_cap) or dlg_out.table.rowCount()) == 2)

    b1 = svc.stock.total_quantity(pid_10k)
    b2 = svc.stock.total_quantity(pid_cap)
    dlg_out.table.cellWidget(0, 1).setValue(3)
    dlg_out.table.cellWidget(1, 1).setValue(4)
    dlg_out.ed_ref.setText("测试项目")
    dlg_out._on_ok()
    pump(150)
    check("多器件一次出库成功",
          svc.stock.total_quantity(pid_10k) == b1 - 3
          and svc.stock.total_quantity(pid_cap) == b2 - 4,
          f"{b1}->{svc.stock.total_quantity(pid_10k)} / "
          f"{b2}->{svc.stock.total_quantity(pid_cap)}")
    check("用途写进了流水",
          svc.db.query_one(
              "SELECT COUNT(*) AS n FROM stock_log WHERE ref='测试项目'")["n"] == 2)

    # ---- 出库：指定批次 ----
    lots = [lot for lot in svc.stock.lots(pid_10k) if lot.quantity > 0]
    first_lot = lots[0]
    dlg_out2 = StockOutDialog(svc, preset_part_id=pid_10k)
    pump(150)
    combo = dlg_out2.table.cellWidget(0, 2)
    check("批次下拉里有具体批次可指定",
          combo.findData(first_lot.id) >= 0, f"{combo.count()} 项")
    combo.setCurrentIndex(combo.findData(first_lot.id))
    dlg_out2.table.cellWidget(0, 1).setValue(2)
    dlg_out2._on_ok()
    pump(150)
    after_lot = svc.stock.lot(first_lot.id)
    check("指定批次时就从那一批扣",
          after_lot.quantity == first_lot.quantity - 2,
          f"{first_lot.quantity} -> {after_lot.quantity}")

    # ---- 出库：全有才出 ----
    dlg_out3 = StockOutDialog(svc, preset_part_id=pid_10k)
    pump(150)
    dlg_out3.add_part(pid_cap)
    pump(150)
    dlg_out3.table.cellWidget(0, 1).setValue(1)
    dlg_out3.table.cellWidget(1, 1).setValue(999999)     # 故意超量
    before_all = (svc.stock.total_quantity(pid_10k), svc.stock.total_quantity(pid_cap))
    dlg_out3._on_ok()
    pump(150)
    after_all = (svc.stock.total_quantity(pid_10k), svc.stock.total_quantity(pid_cap))
    check("有一行超量时整批取消", after_all == before_all,
          f"{before_all} -> {after_all}")

    # ---- 出库：点「＋ 添加器件」真的会开选择器 ----
    # 回归：QPushButton.clicked 带一个 bool 参数。add_part 的首个形参是 part_id，
    # 直接 connect(self.add_part) 时 PySide6 会把 False 传成 part_id，
    # `if part_id is None` 判不过 -> 不弹选择器也不报错，按钮看着完全没反应。
    import app.ui.stock_dialog as sd
    opened: list[int] = []
    real_picker = sd.PartPickerDialog

    class _StubPicker:
        """替掉真选择器：不 exec 模态循环，直接返回预定器件。"""

        next_id: int | None = None

        def __init__(self, services, parent=None):
            self.part_id = _StubPicker.next_id

        def exec(self) -> int:
            opened.append(1)
            return 1

    sd.PartPickerDialog = _StubPicker
    try:
        dlg_click = StockOutDialog(svc)
        _StubPicker.next_id = pid_cap
        dlg_click.btn_add.click()          # 模拟真实点按，会带上 clicked 的 bool
        pump(150)
        check("点「＋ 添加器件」能打开选择器（bool 参数没被当成器件 id）",
              len(opened) == 1, f"选择器 exec 调用了 {len(opened)} 次")
        check("选择器确认后器件进了表格", dlg_click.table.rowCount() == 1)

        _StubPicker.next_id = pid_10k      # 第二个器件的 id
        dlg_click.add_part(False)          # 直接喂 bool
        pump(150)
        check("bool 传给 add_part 时按「没指定器件」处理",
              len(opened) == 2 and dlg_click.table.rowCount() == 2,
              f"exec {len(opened)} 次 / {dlg_click.table.rowCount()} 行")
    finally:
        sd.PartPickerDialog = real_picker

    # ---- 出库 · 成本：FIFO 实际扣减价 + 缺价就地补 ----
    from app.ui.stock_dialog import (
        COL_OUT_SUBTOTAL, ROLE_OUT_MISSING, ROLE_OUT_SUBTOTAL,
    )

    cost_part = svc.parts.create(Part(name="成本测试电阻", footprint="0603"))
    svc.stock.add_lot(cost_part, 100, None, unit_price=0.0100,
                      purchase_date="2026-01-01")
    svc.stock.add_lot(cost_part, 300, None, unit_price=0.0200,
                      purchase_date="2026-02-01")
    np_part = svc.parts.create(Part(name="成本测试无价电阻", footprint="0603"))
    svc.stock.add_lot(np_part, 50, None, unit_price=0.0, purchase_date="2026-03-01")

    dlg_c = StockOutDialog(svc)
    dlg_c.add_part(cost_part)
    pump(150)

    check("出库表格多了单价和小计两列", dlg_c.table.columnCount() == 6)
    # 数量 1：FIFO 只碰到 0.01 那一批
    check("数量 1 时小计 = 该批次单价",
          abs(dlg_c.table.item(0, COL_OUT_SUBTOTAL).data(ROLE_OUT_SUBTOTAL) - 0.01) < 1e-9,
          repr(dlg_c.table.item(0, COL_OUT_SUBTOTAL).data(ROLE_OUT_SUBTOTAL)))
    check("正常行不着色",
          dlg_c.table.item(0, 0).background().color().name().lower() == "#000000")

    # 数量 150：跨两个批次 100@0.01 + 50@0.02 = 2.00，均价 0.013333…
    dlg_c.table.cellWidget(0, 1).setValue(150)
    pump(150)
    check("跨批次小计 = 各批次实际被扣的单价加总",
          abs(dlg_c.table.item(0, COL_OUT_SUBTOTAL).data(ROLE_OUT_SUBTOTAL) - 2.0) < 1e-9,
          repr(dlg_c.table.item(0, COL_OUT_SUBTOTAL).data(ROLE_OUT_SUBTOTAL)))
    check("跨批次时单价列显示加权平均",
          dlg_c.table.item(0, 4).text() == format_money(2.0 / 150),
          dlg_c.table.item(0, 4).text())
    check("跨批次但都有价 → 不算缺价",
          dlg_c.table.item(0, 4).data(ROLE_OUT_MISSING) is False)

    # 指定批次：只用那一批的价
    lot_combo = dlg_c.table.cellWidget(0, 2)
    second_lot = sorted(lot.id for lot in svc.stock.lots(cost_part))[1]
    lot_combo.setCurrentIndex(lot_combo.findData(second_lot))
    pump(150)
    check("指定批次时小计就是那一批的价",
          abs(dlg_c.table.item(0, COL_OUT_SUBTOTAL).data(ROLE_OUT_SUBTOTAL)
              - 150 * 0.02) < 1e-9)
    lot_combo.setCurrentIndex(0)          # 回到自动
    pump(150)

    # 缺价的器件：整行黄底、单价可编辑、合计变黄
    dlg_c.add_part(np_part)
    pump(150)
    check("没记价格的行判为缺价", dlg_c.table.item(1, 4).data(ROLE_OUT_MISSING) is True)
    check("缺价行整行黄底",
          dlg_c.table.item(1, 0).background().color().name().lower()
          == QColor(theme.pal().bg_low).name().lower())
    check("缺价行单价可编辑",
          bool(dlg_c.table.item(1, 4).flags() & Qt.ItemIsEditable))
    check("合计变黄并列出没价的行数",
          dlg_c.lbl_total.styleSheet()
          == f"color: {theme.pal().fg_attention}; font-weight: 500;"
          and "1 行没价" in dlg_c.lbl_total.text(),
          dlg_c.lbl_total.text())

    # 就地补价 → 写该器件所有在库批次 → 行褪色、合计变绿
    dlg_c.table.item(1, 4).setText("0.05")
    pump(200)
    check("补价写到该器件所有在库批次",
          all(abs(lot.unit_price - 0.05) < 1e-9
              for lot in svc.stock.lots(np_part) if lot.quantity > 0))
    check("补价后那行不再缺价", dlg_c.table.item(1, 4).data(ROLE_OUT_MISSING) is False)
    check("补价后行底色恢复",
          dlg_c.table.item(1, 0).background().color().name().lower() == "#000000")
    check("补价后合计变绿",
          dlg_c.lbl_total.styleSheet().startswith(f"color: {theme.pal().fg_ok}"))

    # 坏输入不能写库
    before_np = [lot.unit_price for lot in svc.stock.lots(np_part)]
    dlg_c.table.item(1, 4).setText("abc")
    pump(200)
    check("价格填成文字会被拦下",
          [lot.unit_price for lot in svc.stock.lots(np_part)] == before_np
          and dlg_c.table.item(1, 4).text() == "0.05",
          dlg_c.table.item(1, 4).text())
    check("出库面板有补过价要留痕", dlg_c.changed_price is True)
    dlg_c.close()

    # ---- 回归：数量超过批次余量时，单价不能被稀释 ----
    # 指定批次后把数量填超过余量，扣减会被截断到现有量。
    # 以前单价 = 小计 / **填的数量**，数量越大单价被稀释得越低，
    # 而小计停在现有量的钱 —— 看起来就是"单价在降、总价不动"。
    cap_only = svc.parts.create(Part(name="单价稀释回归", footprint="0603"))
    svc.stock.add_lot(cap_only, 100, None, unit_price=0.02, purchase_date="2026-01-01")
    dlg_cap = StockOutDialog(svc)
    dlg_cap.add_part(cap_only)
    pump(150)
    dlg_cap.table.cellWidget(0, 1).setValue(100)
    pump(150)
    unit_at_100 = dlg_cap.table.item(0, 4).text()
    dlg_cap.table.cellWidget(0, 1).setValue(500)      # 远超余量
    pump(150)
    check("数量超过余量时单价不再被稀释",
          dlg_cap.table.item(0, 4).text() == unit_at_100 == "0.02",
          f"{unit_at_100} -> {dlg_cap.table.item(0, 4).text()}")
    check("超量时小计停在现有量的钱",
          dlg_cap.table.item(0, 5).text() == "2",
          dlg_cap.table.item(0, 5).text())
    check("超量时小计会说明只是现有量的钱",
          "现有这些" in dlg_cap.table.item(0, 5).toolTip(),
          dlg_cap.table.item(0, 5).toolTip())
    dlg_cap.close()

    # ================================================================
    print("\n左侧边栏 · 折叠 / 未分类节点 / 删除反馈")

    # ---- 侧栏折叠 ----
    check("侧栏存在且「分类」面板默认展开",
          hasattr(win, "side_rail") and not win.category_pane.isHidden())
    win.toggle_category_pane()
    check("点折叠后「分类」面板隐藏", win.category_pane.isHidden())
    check("折叠状态写进 settings.json",
          app_config.load().get("category_visible") is False)
    win.toggle_category_pane()
    check("再点恢复展开", not win.category_pane.isHidden())
    check("展开状态写回 settings.json",
          app_config.load().get("category_visible") is True)

    # ---- 「未分类」虚拟节点 ----
    from app.ui.main_window import ROLE_ID as WIN_ROLE_ID, UNCATEGORIZED_ID

    win._clear_filters()
    probe_uncat = svc.parts.create(Part(name="未分类节点探针"))
    win.reload_all()
    root = win.tree.topLevelItem(0)
    uncat_nodes = [root.child(i) for i in range(root.childCount())
                   if root.child(i).data(0, WIN_ROLE_ID) == UNCATEGORIZED_ID]
    check("存在未分类器件时出现「未分类」节点", len(uncat_nodes) == 1)
    want_uncat = svc.search.count(SearchQuery(uncategorized=True))
    check("节点标签带计数", uncat_nodes[0].text(0) == f"未分类（{want_uncat}）",
          uncat_nodes[0].text(0))
    win.tree.setCurrentItem(uncat_nodes[0])
    win._reload_table()
    check("选中未分类节点只显示未分类器件",
          win.model.rowCount() == want_uncat,
          f"界面 {win.model.rowCount()} / 预期 {want_uncat}")
    check("未分类探针就在其中",
          any(win.model.row_at(i).id == probe_uncat
              for i in range(win.model.rowCount())))

    # 全部归类后节点消失
    resistor_cat = next(c.id for c in svc.tree.list_categories() if c.name == "电阻")
    for row in svc.db.query("SELECT id, name FROM part WHERE category_id IS NULL"):
        svc.parts.update(Part(id=row["id"], name=row["name"],
                              category_id=resistor_cat))
    win.reload_all()
    root = win.tree.topLevelItem(0)
    check("全部归类后「未分类」节点消失",
          not any(root.child(i).data(0, WIN_ROLE_ID) == UNCATEGORIZED_ID
                  for i in range(root.childCount())))
    win.tree.setCurrentItem(root)
    win._reload_table()

    # ---- 删除：修掉"静默失效" ----
    # 场景：详情盯着的器件被当前筛选挡住（表格里没选中行）—— 也要能删
    doomed = svc.parts.create(Part(name="待删静默回归器件"))
    win.reload_all()
    win._selected_part_id = doomed
    win.table.clearSelection()
    check("场景就位：表格里没有选中行",
          not win.table.selectionModel().hasSelection())
    win.delete_selected_part()      # QMessageBox.question 已在顶部打桩为 Yes
    check("没有表格选中行也能删（静默失效已修）", svc.parts.get(doomed) is None)

    # 没选中时：按钮路径给提示、快捷键路径安静
    asked: list[str] = []
    real_info = QMessageBox.information
    QMessageBox.information = staticmethod(
        lambda *a, **k: asked.append(a[1] if len(a) > 1 else ""))
    try:
        win._selected_part_id = None
        win.delete_selected_part(notify=True)     # 按钮路径
        check("按钮路径没选中会给提示", len(asked) == 1, str(asked))
        win.delete_selected_part()                # 快捷键路径（静默）
        check("快捷键路径同场景保持安静", len(asked) == 1, str(asked))
    finally:
        QMessageBox.information = real_info

    # ================================================================
    print("\n设置面板")
    from PySide6.QtGui import QKeySequence
    from app.ui.settings_dialog import SettingsDialog

    hook_calls: list[str] = []

    def fake_hk_apply(spec: str):
        hook_calls.append(spec)
        return True, f"已生效：{spec.upper()}"

    def fake_hk_fail(spec: str):
        return False, "热键注册失败，可能已被其它程序占用"

    def fake_hk_current():
        return "CTRL + ALT + E"

    settings = SettingsDialog(svc, win, hotkey_apply=fake_hk_apply,
                              hotkey_current=fake_hk_current)
    check("设置面板两个页签（器件资料页已移除）",
          settings.tabs.count() == 2
          and [settings.tabs.tabText(i) for i in range(2)] == ["常规", "分类管理"],
          str([settings.tabs.tabText(i) for i in range(settings.tabs.count())]))
    check("数据库地址显示为 data 目录",
          settings.ed_db_dir.text() == str(svc.db.db_path.parent),
          settings.ed_db_dir.text())
    from app.paths import log_dir
    check("日志地址显示为 log 目录",
          settings.ed_log_dir.text() == str(log_dir()))

    opened_dirs: list[str] = []
    settings._open_folder = lambda p: opened_dirs.append(str(p))
    settings._open_folder(svc.db.db_path.parent)
    check("「打开文件夹」走可覆写方法（测试不碰 Qt 静态方法）",
          opened_dirs == [str(svc.db.db_path.parent)], str(opened_dirs))

    settings.ed_hotkey.setKeySequence(QKeySequence("Ctrl+Alt+Q"))
    settings._apply_hotkey()
    check("换键成功：hook 收到规范化写法", hook_calls == ["ctrl+alt+q"], str(hook_calls))
    check("换键成功后清空输入框", settings.ed_hotkey.keySequence().isEmpty())

    settings_bad = SettingsDialog(svc, win, hotkey_apply=fake_hk_fail,
                                  hotkey_current=fake_hk_current)
    settings_bad.ed_hotkey.setKeySequence(QKeySequence("Ctrl+Alt+Q"))
    settings_bad._apply_hotkey()
    check("换键失败会显示原因", "占用" in settings_bad.lbl_hotkey_status.text(),
          settings_bad.lbl_hotkey_status.text())
    settings_bad.close()

    settings_ro = SettingsDialog(svc, win)      # 没有 hooks（单测环境）
    check("无 hooks 时热键区只读（不给假按钮）",
          not settings_ro.btn_apply_hotkey.isEnabled())
    settings_ro.close()

    # 应用内快捷键：改 → 应用 → QAction 与 settings.json 同步 → 恢复默认
    settings.ed_sc_new.setKeySequence(QKeySequence("Ctrl+M"))
    settings._apply_inapp_shortcuts()
    check("应用内快捷键写进 QAction",
          win._act_new.shortcut().toString() == "Ctrl+M",
          win._act_new.shortcut().toString())
    check("应用内快捷键写进 settings.json",
          app_config.load().get("shortcut_new_part") == "Ctrl+M")
    settings._reset_inapp_shortcuts()
    check("恢复默认后回到 Ctrl+N",
          win._act_new.shortcut().toString() == "Ctrl+N"
          and app_config.load().get("shortcut_new_part") == "Ctrl+N")

    # 分类页的器件入口：双击编辑 / 「添加器件到分类」。
    # 这里把 PartEditorDialog 换成**假类** —— 真表单是模态的，无头环境没人点
    # 会挂死（模块属性替换在这两个方法里有效：它们运行时才查全局名）。
    from app.ui import settings_dialog as sd_mod
    editor_calls: list[tuple] = []

    class _FakeEditor:
        def __init__(self, parts, labels, part_id=None, default_category_id=None,
                     default_name="", services=None, parent=None):
            editor_calls.append((part_id, default_category_id))

        def exec(self):
            return False           # 模拟"用户取消"，不触发刷新路径

    probe_entry = svc.parts.create(Part(name="分类入口探针"))
    real_editor = sd_mod.PartEditorDialog
    sd_mod.PartEditorDialog = _FakeEditor
    try:
        settings._edit_part_from_category(probe_entry)
        check("分类页双击器件 → 打开编辑表单并带上该器件 id",
              editor_calls[-1] == (probe_entry, None), str(editor_calls))

        some_cat = svc.db.query_one("SELECT id FROM category LIMIT 1")["id"]
        settings._add_part_from_category(some_cat)
        check("分类页「添加器件」→ 新建表单且预选当前分类",
              editor_calls[-1] == (None, some_cat), str(editor_calls))
    finally:
        sd_mod.PartEditorDialog = real_editor
        svc.parts.delete(probe_entry)
        pump(50)

    # 分类管理页：新建一级 → 右栏器件清单 → 删一级（器件变未分类，不删器件）
    from app.ui.category_manager import ROLE_CAT_ID, ROLE_PART_ID
    from PySide6.QtWidgets import QInputDialog

    cat_mgr = settings.category_manager
    n_l1 = cat_mgr.list_l1.count()
    real_gettext = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("冒烟一级分类", True))
    try:
        cat_mgr._add_l1()
        pump(100)
    finally:
        QInputDialog.getText = real_gettext
    check("分类管理能新建一级", cat_mgr.list_l1.count() == n_l1 + 1)
    new_l1 = svc.db.query_one("SELECT id FROM category WHERE name='冒烟一级分类'")
    check("一级分类落库", new_l1 is not None)

    cat_mgr.reload()
    for i in range(cat_mgr.list_l1.count()):
        if cat_mgr.list_l1.item(i).data(ROLE_CAT_ID) == new_l1["id"]:
            cat_mgr.list_l1.setCurrentRow(i)
            break
    pump(50)

    # 空态：右栏给出"还没有器件"的占位行（不可选中），不再是一栏空白
    from PySide6.QtCore import Qt as _Qt
    _empty_flags = cat_mgr.list_parts.item(0).flags()
    check("空分类右栏给占位提示行",
          cat_mgr.list_parts.count() == 1
          and not (_empty_flags & _Qt.ItemFlag.ItemIsSelectable)
          and "还没有器件" in cat_mgr.list_parts.item(0).text())

    # 往该分类挂一个器件 → 右栏列出它（带库存数）
    probe_cat = svc.parts.create(Part(name="分类器件探针",
                                      category_id=new_l1["id"]))
    cat_mgr.reload()
    for i in range(cat_mgr.list_l1.count()):
        if cat_mgr.list_l1.item(i).data(ROLE_CAT_ID) == new_l1["id"]:
            cat_mgr.list_l1.setCurrentRow(i)
            break
    pump(50)
    check("右栏列出挂在该分类下的器件",
          cat_mgr.list_parts.count() == 1
          and cat_mgr.list_parts.item(0).text().startswith("分类器件探针（0）")
          and cat_mgr.list_parts.item(0).data(ROLE_PART_ID) == probe_cat)

    # 双击行 / 「添加器件」按钮 → 宿主槽会开**模态**编辑器（真实行为）。
    # 构造 settings 时旧槽已经连在信号上，必须先断开，再连探针 ——
    # 只验证"信号带对了参数"，绝不真开对话框（模态循环没人点会挂死）。
    cat_mgr.part_activated.disconnect()
    cat_mgr.add_part_requested.disconnect()
    opened: list[tuple[str, int]] = []
    cat_mgr.part_activated.connect(lambda pid: opened.append(("edit", pid)))
    cat_mgr.add_part_requested.connect(lambda cid: opened.append(("add", cid)))
    cat_mgr._on_part_double_clicked(cat_mgr.list_parts.item(0))
    check("双击器件行触发宿主编辑（带器件 id）",
          opened == [("edit", probe_cat)], f"opened={opened}")
    cat_mgr._add_part_to_current()
    check("「添加器件」按钮触发宿主新建（预选当前分类）",
          opened == [("edit", probe_cat), ("add", new_l1["id"])],
          f"opened={opened}")

    # 删一级分类：挂在它下面的器件**不删**，只是变未分类（口径不变）
    cat_mgr._delete_l1()        # QMessageBox.question 已打桩 Yes
    pump(100)
    check("删一级分类生效",
          svc.db.query_one("SELECT id FROM category WHERE id = ?",
                           (new_l1["id"],)) is None)
    check("器件没被删，只是变成未分类",
          svc.parts.get(probe_cat) is not None
          and svc.parts.get(probe_cat).category_id is None)
    check("分类增删后标记 parts_changed（关面板时主窗口整体刷新）",
          settings.parts_changed is True)
    svc.parts.delete(probe_cat)  # 清理探针
    settings.close()

    for dialog in (dlg_in, dlg_in2, dlg_out, dlg_out2, dlg_out3, dlg_click,
                   dlg_price, dlg_price2):
        dialog.close()
    app_config.CONFIG_PATH = original_cfg

    quick.close()
    win._force_close = True
    win.close()
    svc.close()

    print(f"\n{'=' * 62}\n界面逻辑全部通过：{PASSED} 项断言\n{'=' * 62}")


if __name__ == "__main__":
    main()
