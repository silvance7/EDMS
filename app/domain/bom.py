"""BOM 解析与匹配。

**设计原则：强参数卡死，弱参数提示，人来定夺。**

    强参数不符（封装、数值）  -> **直接不进候选**。
        0603 的 10uF 和 0805 的 10uF 焊不到同一个盘上，列出来只会诱导选错。
    弱参数不吻合（型号、名称）-> 只在界面上标 ⚠，点开看原因再决定。
        各家命名本来就有差异，不该一票否决。
    没有候选                  -> 判为缺料。

这条规则照立创商城 BOM 配单的做法定的 —— 官方说明原话：
"如 BOM 中指定 0805，则仅匹配封装为 0805 的，即使参数完全相同，
0603 或 1206 均不列入结果"。

匹配度分数**只在内部用来排序**，不展示给用户：看到"78 分"没法拿它做决定，
看到 ✓ / ⚠ 才有用。

**为什么归一化是地基而不是优化项**（真实 BOM 实测，44 行）::

    BOM 里的封装      C0603 / R0603 / LED_0603 / LQFP-48_L7.0-W7.0-P0.50-LS9.0-BL
    库存库里的封装     0603  / 0603  / 0603     / LQFP-48
    精确相等的行数     0 / 44

数值归一化解决的是「0.1uF」和「100nF」其实是同一种东西这类问题。

本模块**不 import 任何 Qt**，可以脱离界面直接测。
"""

from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# ===========================================================================
#  匹配规则 —— 分三层，对齐立创商城 BOM 配单的做法
#
#    强参数不符   封装、数值对不上 -> **直接不列入候选**
#                 立创的官方说明："如 BOM 中指定 0805，则仅匹配封装为 0805 的，
#                 即使参数完全相同，0603 或 1206 均不列入结果"。
#                 理由很实在 —— 0603 的 10uF 和 0805 的 10uF 焊不到同一个盘上。
#
#    弱参数不吻合 型号、名称对不上 -> 只标 ⚠ 警告图标，
#                 用户点开看原因再决定。各家命名本来就有差异，不该一票否决。
#
#    没有候选     判为缺料。
#
#  分数**只在内部用来排序**（匹配度高的排前面），不展示给用户 ——
#  看到"78 分"没法拿它做决定，看到"✓ / ⚠"才有用。
# ===========================================================================

W_SUPPLIER = 60              # LCSC 编号命中 —— 最强的信号
W_MPN_EXACT = 55             # 厂商型号完全相等
W_MPN_PARTIAL = 25           # 型号互为子串
W_VALUE = 35                 # 归一化数值一致
W_FOOTPRINT = 25             # 归一化封装一致
W_NAME_INCLUDES = 25         # Comment 整串出现在器件名里
W_TOKEN_EACH = 15            # 每个命中 token
W_TOKEN_CAP = 30             # token 部分的总上限
W_NAME_CAP = 40              # 名称类信号合并后的总上限

SCORE_SHOW = 25              # >= 这个分数才作为候选列出来
MAX_CANDIDATES = 8           # 每行最多列几个候选
NEAR_MISS_CAP = 3            # 因强参数被排除的，最多提示几条


# ===========================================================================
#  数据结构
# ===========================================================================

@dataclass(slots=True)
class BomLine:
    index: int                # 行号（从 1 开始，不含表头）
    qty: int
    comment: str
    designators: str
    footprint: str
    value: str
    mpn: str
    manufacturer: str
    supplier_part: str
    supplier: str

    @property
    def label(self) -> str:
        """人看的名字：优先 Comment，退回型号。"""
        return self.comment or self.mpn or self.value or f"第 {self.index} 行"

    @property
    def value_text(self) -> str:
        """用来做数值匹配的文本。不同工具导出时，Comment 和 Value 经常只有一个填了。"""
        return self.comment or self.value


@dataclass(slots=True)
class BomCandidate:
    """一个候选项：某个库存器件对某行 BOM 的匹配情况。

    强参数（封装/数值）在进候选之前已经卡过了，所以这里的每个候选
    封装和数值都是对得上的；`gaps` 记的是**弱参数**没对上之处。
    """

    score: int                      # 只用于排序，不展示
    part_id: int
    part_name: str
    part_footprint: str
    available: int
    evidence: list[str] = field(default_factory=list)   # 正向依据
    gaps: list[str] = field(default_factory=list)       # 弱参数没对上之处

    @property
    def exact(self) -> bool:
        """完全匹配：强参数过了，弱参数也全对得上。"""
        return not self.gaps

    @property
    def evidence_text(self) -> str:
        return " · ".join(self.evidence)


@dataclass(slots=True)
class BomMatch:
    """一行 BOM 的匹配结果。

    `candidates` 按匹配度从高到低排，`part_id` 是预选的（= 第一个），
    界面把整个列表摆出来让用户自己定。
    """

    line: BomLine
    candidates: list[BomCandidate] = field(default_factory=list)
    part_id: int | None = None
    near_miss: list[str] = field(default_factory=list)   # 因强参数不符被排除的
    note: str = ""

    @property
    def best(self) -> BomCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def selected(self) -> BomCandidate | None:
        if self.part_id is None:
            return None
        for c in self.candidates:
            if c.part_id == self.part_id:
                return c
        return None

    @property
    def matched(self) -> bool:
        return self.part_id is not None

    @property
    def available(self) -> int:
        c = self.selected
        return c.available if c else 0

    @property
    def exact(self) -> bool:
        """当前选中的候选是不是完全匹配。"""
        c = self.selected
        return bool(c and c.exact)

    @property
    def gaps(self) -> list[str]:
        c = self.selected
        return c.gaps if c else []

    @property
    def score(self) -> int:
        c = self.selected
        return c.score if c else 0

    @property
    def state(self) -> str:
        """界面用：ok=完全匹配 / check=需人工核对 / missing=没匹配上。"""
        if self.part_id is None:
            return "missing"
        return "ok" if self.exact else "check"

    @property
    def enough(self) -> bool:
        """库存数量够不够。"""
        return self.matched and self.available >= self.line.qty

    @property
    def ready(self) -> bool:
        """可以放心参与出库：**完全匹配且库存够**。

        这是界面默认勾选「参与」的判据。注意"完全匹配"用的是
        ✓（弱参数也全对上），而不是"分数够高" —— 分数是内部排序用的，
        拿它当准入门槛等于把判断权交回给算法，和当初的设计初衷相反。
        """
        return self.exact and self.enough


# ===========================================================================
#  成本折算：这套板子要花多少钱
# ===========================================================================

@dataclass(slots=True)
class CostLine:
    """一行 BOM 的钱。界面直接拿它铺表。"""

    index: int                     # BOM 行号
    label: str                     # 人看的名字
    qty: int
    part_id: int | None = None
    part_name: str = ""
    unit_price: float = 0.0        # 单颗均价；0 = 库里没记
    in_stock: int = 0              # 该器件在库总量

    @property
    def matched(self) -> bool:
        return self.part_id is not None

    @property
    def subtotal(self) -> float:
        return self.qty * self.unit_price

    @property
    def missing_price(self) -> bool:
        """匹配到了器件，但库里没记价格 —— 界面上标黄的那一类。"""
        return self.matched and self.unit_price <= 0

    @property
    def editable(self) -> bool:
        """能不能在这儿改价：得有器件，而且得有**在库**批次可写。

        价格记在批次上，改价只写 `quantity > 0` 的批次，所以判据是在库总量，
        不是批次数 —— `lot_count` 把数量已经归零的批次也算进去了，用它当判据
        会出现"显示可改、实际改不动"。器件完全没在库批次就只能先去入库。
        """
        return self.matched and self.in_stock > 0


@dataclass(slots=True)
class CostReport:
    """整份 BOM 的钱，外加"这个钱算得全不全"。"""

    lines: list[CostLine] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(l.subtotal for l in self.lines)

    @property
    def missing(self) -> list[CostLine]:
        """有器件、但库里没价格。"""
        return [l for l in self.lines if l.missing_price]

    @property
    def unmatched(self) -> list[CostLine]:
        """连器件都没匹配上，同样算不进钱。"""
        return [l for l in self.lines if not l.matched]

    @property
    def complete(self) -> bool:
        """全都算得出来才算"齐了"。缺料也算不齐 —— 缺料的钱一样是不知道的。"""
        return not self.missing and not self.unmatched

    @property
    def ordered(self) -> list[CostLine]:
        """有问题的置顶：先「有器件没价格」，再「没匹配上」，最后按行号。"""
        def key(line: CostLine) -> tuple[int, int]:
            if line.missing_price:
                return (0, line.index)
            if not line.matched:
                return (1, line.index)
            return (2, line.index)

        return sorted(self.lines, key=key)

    def unit_price_of(self, part_id: int) -> float:
        """某个器件在这份 BOM 里算的单价（同一器件各行应当同价）。"""
        for line in self.lines:
            if line.part_id == part_id:
                return line.unit_price
        return 0.0


def build_cost(matches: list[BomMatch], info) -> CostReport:
    """把匹配结果折算成钱。

    `info(part_id)` 返回 `PartOverview | None`：单价取 `avg_price`（持仓均价，
    多批次价格不同时已按数量加权），在库量取 `total_qty`。

    **单价是"快照"**：调用方在改动价格后必须重建一次，否则总价还是旧的。
    """
    lines: list[CostLine] = []
    for m in matches:
        pid = m.part_id
        overview = info(pid) if pid is not None else None
        lines.append(CostLine(
            index=m.line.index,
            label=m.line.label,
            qty=m.line.qty,
            part_id=pid,
            part_name=overview.name if overview else "",
            unit_price=float(overview.avg_price) if overview else 0.0,
            in_stock=int(overview.total_qty) if overview else 0,
        ))
    return CostReport(lines=lines)


# ===========================================================================
#  数值归一化
# ===========================================================================

# 前缀 -> 倍率。含大写变体，因为 BOM 里 "22UF" / "10K" 这种写法很常见。
_PREFIX = {
    "p": 1e-12, "P": 1e-12,
    "n": 1e-9, "N": 1e-9,
    "u": 1e-6, "U": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3, "K": 1e3,
    "M": 1e6, "G": 1e9,
}

# 只认「数字 + 可选前缀 + 纯字母单位」。
# 注意单位部分**不允许出现数字**，这样 "1N4148W" 这类型号不会被误当成数值。
_VALUE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([pPnuUµμmMkKG]?)\s*([A-Za-zΩω]*)$")

_FAMILY_RES = [
    (re.compile(r"^h$", re.I), "H"),
    (re.compile(r"^f$", re.I), "F"),
    (re.compile(r"^(r|ohm|ω)$", re.I), "R"),
    (re.compile(r"^hz$", re.I), "Hz"),
    (re.compile(r"^v$", re.I), "V"),
    (re.compile(r"^a$", re.I), "A"),
    (re.compile(r"^w$", re.I), "W"),
]

# 封装首字母能提示元件族：R0603 是电阻、C0603 是电容、L 是电感
_FOOTPRINT_FAMILY = {"r": "R", "c": "F", "l": "H"}


@dataclass(frozen=True, slots=True)
class ValueKey:
    """归一化后的数值：换算到 SI 基本单位后的数量 + 元件族。"""

    number: float
    family: str       # "R" / "F" / "H" / "Hz" / "" (未知)

    def __str__(self) -> str:
        return f"{self.number:g}{self.family}"


def parse_value(text: str, footprint: str = "") -> ValueKey | None:
    """把 "0.1uF" / "100nF" / "10K" / "22UF" 解析成统一口径的数值。

    >>> parse_value("0.1uF")
    ValueKey(number=1e-07, family='F')
    >>> parse_value("100nF")
    ValueKey(number=1e-07, family='F')
    >>> parse_value("10K", "R0603")
    ValueKey(number=10000.0, family='R')
    """
    if not text:
        return None

    m = _VALUE_RE.match(text.strip())
    if not m:
        return None

    num = float(m.group(1))
    prefix = _PREFIX.get(m.group(2), None)
    if prefix is None:
        return None
    unit = m.group(3)

    family = ""
    for pattern, fam in _FAMILY_RES:
        if pattern.match(unit):
            family = fam
            break

    if not unit:
        # 没有单位（"10k" / "5.1K"），靠封装首字母猜元件族
        head = footprint.split("_")[0][:1].lower() if footprint else ""
        family = _FOOTPRINT_FAMILY.get(head, "")

    number = num * prefix
    if number == 0:
        return None
    return ValueKey(number, family)


def values_equal(a: ValueKey | None, b: ValueKey | None) -> bool:
    """两个数值是否等价。任一元件族未知时只比数量。"""
    if a is None or b is None:
        return False
    if not math.isclose(a.number, b.number, rel_tol=1e-6):
        return False
    if a.family and b.family and a.family != b.family:
        return False
    return True


def unit_to_value(value_num: float, unit: str) -> ValueKey | None:
    """把库存库里「数值 + 模板单位」转成同一口径。

    >>> unit_to_value(100, "nF")
    ValueKey(number=1e-07, family='F')
    >>> unit_to_value(10000, "Ω")
    ValueKey(number=10000.0, family='R')
    """
    if value_num is None:
        return None
    key = parse_value(f"{value_num:g}{unit}")
    return key


# ===========================================================================
#  封装归一化
# ===========================================================================

# 4 位英制尺寸（0603 / 0805 …）。用前后非数字锁边界，
# 避免把 18650 电池这种 5 位数字切成 "1865"。
_IMPERIAL_RE = re.compile(r"(?<![0-9])(\d{4})(?![0-9])")

# 常见封装名。
# 两个坑都要防：
#   1. 长名必须排在短名前（"tssop" 在 "sop" 前），否则前缀会被短名先吃掉；
#   2. 要**连脚数一起捕获**，而且 SOT-23-5 这种是两段数字。
#      只写一个 (\d+)? 的话 "sot-23-5" 会被截成 "sot-23" ——
#      SOT-23 / SOT-23-3 / SOT-23-5 / SOT-23-6 就全塌成同一种封装了，
#      这是实测中发现并修掉的 bug。
_PACKAGE_RE = re.compile(
    r"^(tssop|ssop|esop|msop|lqfp|tqfp|soic|qfn|dfn|sop|sot|dip|to|sma|smb|smc|"
    r"hc-?49s|usb-c|type-c)"
    r"(?:-(\d+))?(?:-(\d+))?",
    re.I,
)


def primary_footprint(footprint: str) -> str:
    """把封装名归一成**一个**主 token，用于比较。

    只取一个是有意为之：候选越多越容易误判。
    "SOT-23-5_L3.0-..." 的主 token 是 "sot-23-5"，不会退化去匹配 "SOT-23"。

    >>> primary_footprint("C0603")
    '0603'
    >>> primary_footprint("LED_0603")
    '0603'
    >>> primary_footprint("LQFP-48_L7.0-W7.0-P0.50-LS9.0-BL")
    'lqfp-48'
    >>> primary_footprint("SOT-23-5_L3.0-W1.7-P0.95-LS2.8-BL")
    'sot-23-5'
    """
    if not footprint:
        return ""

    fp = footprint.strip().lower()
    # 部分 EDA 工具会写成 "库名:封装名"
    if ":" in fp:
        fp = fp.rsplit(":", 1)[1]

    head = fp.split("_", 1)[0]

    # 1) 封装首字母 + 4 位尺寸：c0603 / r0805 / led0603
    m = re.fullmatch(r"[a-z]{1,3}(\d{4})", head)
    if m:
        return m.group(1)

    # 2) 已知封装名（**脚数必须一起带上**）
    m = _PACKAGE_RE.match(head)
    if m:
        segments = [m.group(1).lower()]
        segments += [g for g in (m.group(2), m.group(3)) if g]
        return "-".join(segments)

    # 3) 整个字符串里找 4 位英制尺寸（处理 LED_0603 这种前缀不是 R/C/L 的）
    m = _IMPERIAL_RE.search(fp)
    if m:
        return m.group(1)

    return head


def footprints_equal(a: str, b: str) -> bool:
    """两个封装是否算同一种。空值视为不参与判断（返回 True，交给数值去决定）。"""
    pa, pb = primary_footprint(a), primary_footprint(b)
    if not pa or not pb:
        return True
    return pa == pb


_SIZE_RE = re.compile(r"^\d{4}$")


def is_standard_footprint(token: str) -> bool:
    """这个归一化后的封装是不是**算法认得的**标准封装。

    只有两侧都是标准封装时，才拿封装去做强校验。原因：
    用户会在 EDA 里自己画封装（`0.96OLED_4P`、`SMD-PCB`、`ROCKER_16*16_KEY_JX`），
    这些名字算法根本没法判断对应关系。硬卡会把"其实有货"错判成缺料 ——
    实测把缺料从 30 行推到了 39 行，其中好几行库里的确有货。
    这类只标警告，让用户自己看。

    >>> is_standard_footprint("0603"), is_standard_footprint("lqfp-48")
    (True, True)
    >>> is_standard_footprint("0.96oled"), is_standard_footprint("hdr-th")
    (False, False)
    """
    if not token:
        return False
    if _SIZE_RE.match(token):          # 0603 / 0805 …
        return True
    return bool(_PACKAGE_RE.match(token))


# ===========================================================================
#  读取 BOM
# ===========================================================================

_HEADER_MAP = {
    "no.": "index", "no": "index", "#": "index", "序号": "index", "编号": "index",
    "quantity": "qty", "qty": "qty", "数量": "qty",
    "comment": "comment", "注释": "comment", "说明": "comment",
    "designator": "designators", "designators": "designators",
    "reference": "designators", "references": "designators", "位号": "designators",
    "footprint": "footprint", "封装": "footprint", "package": "footprint", "库封装": "footprint",
    "value": "value", "值": "value",
    "manufacturer part": "mpn", "manufacturer part number": "mpn", "mpn": "mpn",
    "厂商型号": "mpn", "型号": "mpn",
    "manufacturer": "manufacturer", "厂商": "manufacturer", "制造商": "manufacturer",
    "supplier part": "supplier_part", "supplier part number": "supplier_part",
    "供应商编号": "supplier_part", "供应商料号": "supplier_part",
    "supplier": "supplier", "供应商": "supplier",
}

_FIELDS = ("index", "qty", "comment", "designators", "footprint",
           "value", "mpn", "manufacturer", "supplier_part", "supplier")


def _rows_to_lines(header: list[str], rows: list[list[str]]) -> list[BomLine]:
    mapping: dict[int, str] = {}
    for i, h in enumerate(header):
        key = _HEADER_MAP.get(h.strip().lower())
        if key and key not in mapping.values():
            mapping[i] = key

    if "comment" not in mapping.values() and "mpn" not in mapping.values():
        raise ValueError(
            "认不出 BOM 的列名。至少需要 Comment 或 Manufacturer Part 列。\n"
            f"实际读到：{header}"
        )

    lines: list[BomLine] = []
    for raw in rows:
        if not any(cell.strip() for cell in raw):
            continue
        data = {k: "" for k in _FIELDS}
        for i, cell in enumerate(raw):
            key = mapping.get(i)
            if key:
                data[key] = (cell or "").strip()
        if not (data["comment"] or data["mpn"] or data["value"]):
            continue

        try:
            qty = int(float(data["qty"])) if data["qty"] else 0
        except ValueError:
            qty = 0
        if qty <= 0:
            continue

        try:
            index = int(float(data["index"])) if data["index"] else len(lines) + 1
        except ValueError:
            index = len(lines) + 1

        lines.append(BomLine(
            index=index, qty=qty, comment=data["comment"],
            designators=data["designators"], footprint=data["footprint"],
            value=data["value"], mpn=data["mpn"],
            manufacturer=data["manufacturer"],
            supplier_part=data["supplier_part"], supplier=data["supplier"],
        ))
    return lines


def _read_xlsx(path: Path) -> tuple[list[str], list[list[str]]]:
    """读 xlsx。用标准库解 zip + XML，不引 openpyxl。"""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))

        sheet_names = [n for n in z.namelist()
                       if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
        if not sheet_names:
            raise ValueError("xlsx 里找不到工作表")
        root = ET.fromstring(z.read(sorted(sheet_names)[0]))

        table: list[list[str]] = []
        for row in root.iter(f"{ns}row"):
            cells: dict[int, str] = {}
            for c in row.findall(f"{ns}c"):
                ref = c.get("r") or ""
                letters = re.match(r"([A-Z]+)", ref)
                if not letters:
                    continue
                col = 0
                for ch in letters.group(1):
                    col = col * 26 + (ord(ch) - 64)

                t = c.get("t")
                v = c.find(f"{ns}v")
                inline = c.find(f"{ns}is")
                if t == "s" and v is not None:
                    val = shared[int(v.text or 0)]
                elif inline is not None:
                    val = "".join(x.text or "" for x in inline.iter(f"{ns}t"))
                else:
                    val = (v.text or "") if v is not None else ""
                cells[col - 1] = val.strip()
            if cells:
                width = max(cells) + 1
                table.append([cells.get(i, "") for i in range(width)])

    if not table:
        raise ValueError("xlsx 是空的")
    return table[0], table[1:]


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """读 CSV。不同工具导出的编码不一（utf-8-sig / GBK 都遇到过），都试一遍。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 编码无法识别（试过 utf-8 / gbk / utf-16）")

    reader = csv.reader(io.StringIO(text))
    table = [row for row in reader if any(cell.strip() for cell in row)]
    if not table:
        raise ValueError("CSV 是空的")
    return table[0], table[1:]


def read_bom(path: str | Path) -> list[BomLine]:
    """读 BOM 文件，支持 .xlsx 和 .csv。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"BOM 文件不存在：{path}")

    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        header, rows = _read_xlsx(path)
    elif suffix in (".csv", ".txt", ".tsv"):
        header, rows = _read_csv(path)
    else:
        raise ValueError(f"不支持的 BOM 格式：{suffix}（支持 .xlsx / .csv）")

    return _rows_to_lines(header, rows)


# ===========================================================================
#  匹配器
# ===========================================================================

class BomMatcher:
    """把 BOM 行匹配到库存器件。

    一次性把所有器件读进内存建索引 —— 本应用规模（<= 1 万条）下这比逐行查库
    快得多，也简单得多。索引同时兼任**剪枝**：只对"至少共享一个信号"的器件打分，
    避免每一行都把全库算一遍。
    """

    def __init__(self, services) -> None:
        self.svc = services
        self._load()

    def _load(self) -> None:
        svc = self.svc
        rows = svc.db.query(
            "SELECT id, name, footprint, mpn, keywords FROM part WHERE is_active = 1"
        )
        self.parts: dict[int, dict] = {}
        self.part_values: dict[int, set[str]] = {}
        self.part_tokens: dict[int, set[str]] = {}
        self.by_supplier: dict[str, set[int]] = {}
        self.by_mpn: dict[str, set[int]] = {}
        self.by_value: dict[str, set[int]] = {}
        self.by_token: dict[str, set[int]] = {}

        # 厂商和供应商现在记在**批次**上，而 BOM 里恰恰有"厂商/供应商"这两列。
        # 把它们也索引进 token，这样 BOM 写了厂牌名就能在名称那一路命中。
        lot_meta: dict[int, list[str]] = {}
        for row in svc.db.query(
            "SELECT part_id, manufacturer, supplier FROM stock_lot"
        ):
            bucket = lot_meta.setdefault(row["part_id"], [])
            for value in (row["manufacturer"], row["supplier"]):
                if value:
                    bucket.append(value)

        for r in rows:
            pid = r["id"]
            name = r["name"] or ""
            footprint = r["footprint"] or ""
            mpn = (r["mpn"] or "").strip()
            keywords = r["keywords"] or ""

            supplier_keys = set(_supplier_keys(f"{mpn} {keywords}"))
            tokens = (set(_keyword_tokens(name))
                      | set(_keyword_tokens(keywords))
                      | set(_keyword_tokens(" ".join(lot_meta.get(pid, [])))))

            values: set[str] = set()
            for pv in svc.parts.get_params(pid):
                key = unit_to_value(pv.value_num, pv.unit)
                if key is not None:
                    values.add(_value_bucket(key))
            # 器件名里的数值也算信号（"10kΩ 0603 1% 电阻"）
            name_key = parse_value(_first_number_token(name), footprint)
            if name_key is not None:
                values.add(_value_bucket(name_key))

            self.parts[pid] = {
                "name": name, "footprint": footprint,
                "mpn": mpn, "supplier_keys": supplier_keys,
            }
            self.part_values[pid] = values
            self.part_tokens[pid] = tokens

            for k in supplier_keys:
                self.by_supplier.setdefault(k, set()).add(pid)
            if mpn:
                self.by_mpn.setdefault(mpn.lower(), set()).add(pid)
            for v in values:
                self.by_value.setdefault(v, set()).add(pid)
            for t in tokens:
                self.by_token.setdefault(t, set()).add(pid)

    # ------------------------------------------------------------------

    def match(self, lines: list[BomLine]) -> list[BomMatch]:
        return [self.match_line(line) for line in lines]

    # ------------------------------------------------------------------
    #  强 / 弱参数
    # ------------------------------------------------------------------

    def _strong_gap(self, line: BomLine, pid: int, line_key: ValueKey | None) -> str | None:
        """强参数校验：封装、数值。不符返回原因，符合返回 None。

        对齐立创 BOM 配单的规则："如 BOM 中指定 0805，则仅匹配封装为 0805 的，
        即使参数完全相同，0603 或 1206 均不列入结果"。
        理由很实在 —— 0603 的 10uF 和 0805 的 10uF 焊不到同一个盘上，
        把它列进候选只会诱导用户选错。
        """
        part = self.parts[pid]

        lb = primary_footprint(line.footprint)
        pb = primary_footprint(part["footprint"])
        # 只在**两侧都是标准封装**时才拿封装做强校验。
        # 自定义封装（0.96OLED_4P / SMD-PCB 这种用户自己画的）算法认不出来，
        # 硬卡会把"其实有货"错判成缺料 —— 那种情况交给 _confirm_gaps 标警告。
        if lb and pb and lb != pb:
            if is_standard_footprint(lb) and is_standard_footprint(pb):
                return f"封装不符：BOM 是 {lb}，库里记的是 {pb}"

        if line_key is not None:
            have = self.part_values[pid]
            # 库里这个器件**一个数值都没记** -> 没法核对，不该判死。
            # 交给 _confirm_gaps 标个「无法核对」的警告，让用户去补录数据。
            # 直接排除的话，用户会以为"库里没有"，其实是自己没录参数。
            if have and _value_bucket(line_key) not in have:
                return f"参数不符：BOM 是 {line_key}"

        return None

    def _confirm_gaps(self, line: BomLine, pid: int,
                      line_key: ValueKey | None, line_tokens: set[str]) -> list[str]:
        """弱参数校验：型号、名称，以及"库里根本没记"的项。

        返回"没对上的地方"，空列表 = 完全匹配。
        弱信号不吻合**不代表不是同一个东西**（各家命名本来就有差异），
        所以只标记、不排除，由用户点 ⚠ 看原因后自己定。
        """
        part = self.parts[pid]

        # 供应商编号命中 = 精确身份，直接算完全匹配
        keys = set(_supplier_keys(line.supplier_part))
        if keys and (keys & part["supplier_keys"]):
            return []

        gaps: list[str] = []

        line_mpn = line.mpn.strip().lower()
        part_mpn = part["mpn"].lower()
        if line_mpn and part_mpn:
            if line_mpn == part_mpn:
                return []          # 型号完全一致，也算完全匹配
            gaps.append(f"型号不完全一致（库里是 {part['mpn']}）")

        label = line.label.strip().lower()
        if len(label) >= 2 and label not in part["name"].lower():
            gaps.append(f"名称不完全一致（库里叫「{part['name']}」）")

        missing = sorted(line_tokens - self.part_tokens[pid], key=len, reverse=True)
        if missing:
            gaps.append("名称里找不到 " + " / ".join(missing[:3]))

        # 库存侧数据缺失 -> 无法核对。要明说，不能默默当成"对上了"，
        # 也不能因此判死 —— 那是用户没录，不是库里没有。
        if line_key is not None and not self.part_values[pid]:
            gaps.append("库里这个器件没记数值参数，无法核对")
        if primary_footprint(line.footprint) and not primary_footprint(part["footprint"]):
            gaps.append("库里这个器件没填封装，无法核对")

        # 封装对不上、但没被强校验拦下（因为至少一侧是自定义封装，算法认不出来）
        # —— 差异确实存在，标出来让人工判断
        lb = primary_footprint(line.footprint)
        pb = primary_footprint(part["footprint"])
        if lb and pb and lb != pb:
            gaps.append(f"封装对不上（BOM 是 {lb}，库里是 {pb}），请人工确认")

        return gaps

    # ------------------------------------------------------------------

    def match_line(self, line: BomLine) -> BomMatch:
        supplier_keys = set(_supplier_keys(line.supplier_part))
        line_key = parse_value(line.value_text, line.footprint)
        line_tokens = set(_keyword_tokens(line.value_text or line.mpn))

        candidates: list[BomCandidate] = []
        near_miss: list[str] = []

        for pid in self._candidate_ids(line, supplier_keys, line_key, line_tokens):
            # 强参数不符 -> 不进候选。但记一笔，让用户知道
            # "库里有这个东西，只是记的封装/参数跟 BOM 不一致"，而不是纯缺料。
            gap = self._strong_gap(line, pid, line_key)
            if gap is not None:
                if len(near_miss) < NEAR_MISS_CAP:
                    near_miss.append(f"{self.parts[pid]['name']}　（{gap}）")
                continue

            score, evidence = self._score(line, pid, line_key, line_tokens, supplier_keys)
            if score < SCORE_SHOW:
                continue

            part = self.parts[pid]
            candidates.append(BomCandidate(
                score=score,
                part_id=pid,
                part_name=part["name"],
                part_footprint=part["footprint"],
                available=self.svc.stock.total_quantity(pid),
                evidence=evidence,
                gaps=self._confirm_gaps(line, pid, line_key, line_tokens),
            ))

        # 匹配度降序；同分按名称排序，保证结果稳定可复现
        candidates.sort(key=lambda c: (-c.score, c.part_name))

        result = BomMatch(line=line, candidates=candidates[:MAX_CANDIDATES],
                          near_miss=near_miss)
        if not candidates:
            result.note = "库存里没有能对上的器件"
        else:
            # 一律预选第一个（匹配度最高），完整列表摆出来供你改。
            # "是否默认参与出库"另由 ready（完全匹配 **且** 库存够）决定。
            result.part_id = candidates[0].part_id
            if candidates[0].exact:
                result.note = f"完全匹配，共 {len(candidates)} 个候选"
            else:
                result.note = f"{len(candidates)} 个候选都有项目没对上，点 ⚠ 看原因"
        return result

    # ------------------------------------------------------------------
    #  打分（只用于排序，不展示）
    # ------------------------------------------------------------------

    def _candidate_ids(self, line, supplier_keys, line_key, line_tokens) -> set[int]:
        """剪枝：只对"至少共享一个信号"的器件打分。"""
        ids: set[int] = set()
        for k in supplier_keys:
            ids |= self.by_supplier.get(k, set())
        if line.mpn:
            ids |= self.by_mpn.get(line.mpn.strip().lower(), set())
        if line_key is not None:
            ids |= self.by_value.get(_value_bucket(line_key), set())
        for t in line_tokens:
            ids |= self.by_token.get(t, set())
        return ids

    def _score(self, line, pid, line_key, line_tokens, supplier_keys) -> tuple[int, list[str]]:
        """匹配度打分，**只用来排序**，不展示给用户。

        走到这里的器件强参数（封装/数值）都已经过了，所以这两项直接计分。
        返回的 evidence 是正向依据，点 ✓ 图标时给用户看"凭什么它排前面"。
        """
        part = self.parts[pid]
        score = 0
        evidence: list[str] = []

        hit = supplier_keys & part["supplier_keys"]
        if hit:
            score += W_SUPPLIER
            evidence.append(f"供应商编号 {sorted(hit)[0]}")

        line_mpn = line.mpn.strip().lower()
        part_mpn = part["mpn"].lower()
        if line_mpn and part_mpn:
            if line_mpn == part_mpn:
                score += W_MPN_EXACT
                evidence.append("型号完全一致")
            elif line_mpn in part_mpn or part_mpn in line_mpn:
                score += W_MPN_PARTIAL
                evidence.append("型号部分一致")

        # 注意：强参数过了**不等于**一定"相等"。
        # 库里没记数值、或者封装是自定义认不出来时，强校验会放行，
        # 所以这里仍然要实际比对再计分 —— 否则会出现
        # "封装一致 cap-smd" 这种自相矛盾的匹配依据（封装明明对不上）。
        if line_key is not None and _value_bucket(line_key) in self.part_values[pid]:
            score += W_VALUE
            evidence.append(f"参数一致 {line_key}")

        lb = primary_footprint(line.footprint)
        pb = primary_footprint(part["footprint"])
        if lb and lb == pb:
            score += W_FOOTPRINT
            evidence.append(f"封装一致 {lb}")

        # 名称类信号先合并再封顶。不合并的话 "10uF" 会同时触发
        # "名称含 10uf" 和 "名称直接包含"，同一件事加两次分（实测踩到过）。
        common = sorted(line_tokens & self.part_tokens[pid], key=len, reverse=True)
        name_score = 0
        if common:
            name_score += min(W_TOKEN_EACH * len(common), W_TOKEN_CAP)
            evidence.append("名称含 " + "/".join(common[:3]))

        label = line.label.strip().lower()
        if len(label) >= 2 and label in part["name"].lower():
            name_score += W_NAME_INCLUDES
            evidence.append("名称直接包含")
        score += min(name_score, W_NAME_CAP)

        return max(0, min(score, 100)), evidence


def _value_bucket(key: ValueKey) -> str:
    """数值分桶，用来当索引键。

    用 6 位有效数字规格化以抹掉浮点误差 —— 0.1uF 和 100nF 在二进制下
    不一定逐位相等，格式化之后才是同一个串。
    """
    return f"{key.family}:{key.number:.6g}"


# 同时抓 ASCII 片段和汉字片段，才能把中英混写的值切开
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?|[\u4e00-\u9fff]+")

# 纯英制尺寸（0603 / 0805 …）。它们是**封装**不是器件标识，命中面极广，
# 拿它们当检索词会把一堆 0603 的电容匹配成 LED。
_IMPERIAL_TOKEN = re.compile(r"^(0201|0402|0603|0805|1206|1210|1812|2010|2512)$")

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _keyword_tokens(text: str) -> list[str]:
    """把一段中英混写的文本切成检索 token，**长的排前面**。

    >>> _keyword_tokens("nrf24l01模块")
    ['nrf24l01', '模块']
    >>> _keyword_tokens("LED_0603-G")
    ['led']
    >>> _keyword_tokens("M3螺丝")
    ['螺丝']                      # "m3" 被丢掉，否则会命中 STM32 的 Cortex-M3

    三条过滤规则，都是为了"宁可漏报不可错报"：
      1. 丢掉纯英制尺寸 —— 那是封装，不是标识；
      2. 汉字 2 字起、ASCII 3 字符起 —— 汉字信息密度高，而 "m3"/"4p"/"1k"
         这类 ASCII 两字符太泛，拿去检索会命中一堆无关器件；
      3. 长的排前面，因为放宽时先砍短 token 更安全。
    """
    raw = _TOKEN_RE.findall((text or "").lower())
    seen: set[str] = set()
    tokens: list[str] = []
    for token in raw:
        if token in seen or _IMPERIAL_TOKEN.match(token):
            continue
        minimum = 2 if _CJK_RE.search(token) else 3
        if len(token) < minimum:
            continue
        seen.add(token)
        tokens.append(token)
    tokens.sort(key=len, reverse=True)
    return tokens


_SUPPLIER_RE = re.compile(r"\bC\d{5,}\b", re.I)


def _supplier_keys(text: str) -> list[str]:
    """从一段文本里抠出 LCSC 编号（形如 C8734 / C5311018）。"""
    return [m.group(0).upper() for m in _SUPPLIER_RE.finditer(text or "")]


_NUMBER_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])(\d+(?:\.\d+)?[pnuµmkKMG]?[A-Za-zΩω]{0,3})(?![0-9])")


def _first_number_token(name: str) -> str:
    """从器件名里取第一个像数值的片段，"10kΩ 0603 1% 电阻" -> "10kΩ"。"""
    for m in _NUMBER_TOKEN_RE.finditer(name or ""):
        token = m.group(1)
        if parse_value(token) is not None:
            return token
    return ""
