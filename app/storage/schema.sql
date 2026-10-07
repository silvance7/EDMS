-- ============================================================================
--  电子元器件管理系统 · 数据库结构
--  SQLite 3 · 单文件 · 无服务器 · 全程不联网
--
--  设计要点：
--
--  1) 两级库存模型 —— part（物料主数据）1:N stock_lot（库存批次）
--     同一个器件可以在多个位置各存一批，每批的单价与购买日期独立记录，
--     总库存 = SUM(stock_lot.quantity)。
--     这样"价格"和"购买时间"才有地方安放——它们是批次属性，不是器件属性。
--
--  2) 分类与位置均为树形（parent_id 自引用 + ON DELETE CASCADE）。
--
--  3) 参数用「模板 + 取值」两表（EAV 的一种特化）：
--     param_template 由分类定义（电阻才有"阻值"，电容才有"容值"），
--     part_param 存具体取值，数值列与文本列分开，以便做范围筛选。
--
--  4) search_text 是冗余检索列，由仓储层在写入时统一拼装并小写化。
--     之所以不用 FTS5：unicode61 分词器不切分中文（"电阻"整块成一个词，
--     搜"电"匹配不到），trigram 分词器又要求查询词 >= 3 字符，中文两字词失效。
--     本应用规模（<= 1 万条）下 LIKE 全表扫描约 5-20ms，已满足 50ms 目标，
--     且行为可预期、零踩坑。数据量突破 5 万条再引入 FTS5 不迟。
--
--  5) stock_log 记录每一次出入库，既是审计轨迹，也是库存数的重建依据。
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 元信息（schema 版本等）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 分类树：电阻 / 电容 / 连接器 / 芯片 ...
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS category (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    parent_id  INTEGER REFERENCES category(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    note       TEXT    NOT NULL DEFAULT ''
);

-- 同一父节点下不允许重名。用 IFNULL 把顶层的 NULL 归一成 0，
-- 否则 SQLite 认为 NULL 互不相等，顶层可以插入无数个同名分类。
CREATE UNIQUE INDEX IF NOT EXISTS ux_category_name
    ON category (IFNULL(parent_id, 0), name);
CREATE INDEX IF NOT EXISTS ix_category_parent ON category (parent_id);

-- ---------------------------------------------------------------------------
-- 器件（物料主数据）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS part (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,            -- 例如 "10kΩ 0603 1% 电阻"
    category_id  INTEGER REFERENCES category(id) ON DELETE SET NULL,
    mpn          TEXT    NOT NULL DEFAULT '', -- 型号 / 料号，如 STM32F103C8T6
    footprint    TEXT    NOT NULL DEFAULT '', -- 封装，如 0603 / LQFP-48
    description  TEXT    NOT NULL DEFAULT '',
    datasheet    TEXT    NOT NULL DEFAULT '', -- 数据手册路径或链接
    keywords     TEXT    NOT NULL DEFAULT '', -- 别名，空格分隔
    search_text  TEXT    NOT NULL DEFAULT '', -- 冗余检索列，仓储层维护
    min_stock    INTEGER NOT NULL DEFAULT 0,  -- 低于此值预警
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_part_category  ON part (category_id);
CREATE INDEX IF NOT EXISTS ix_part_footprint ON part (footprint);
CREATE INDEX IF NOT EXISTS ix_part_active    ON part (is_active);

-- ---------------------------------------------------------------------------
-- 参数模板：由分类定义——该分类下的器件应该有哪些规格字段
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS param_template (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER REFERENCES category(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,               -- 阻值 / 精度 / 耐压
    unit        TEXT    NOT NULL DEFAULT '',    -- Ω / % / V
    data_type   TEXT    NOT NULL DEFAULT 'num', -- num | text
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_template_name
    ON param_template (IFNULL(category_id, 0), name);
CREATE INDEX IF NOT EXISTS ix_template_category ON param_template (category_id);

-- ---------------------------------------------------------------------------
-- 参数取值：某个器件在某个模板上的具体值
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS part_param (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id     INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
    template_id INTEGER NOT NULL REFERENCES param_template(id) ON DELETE CASCADE,
    value_num   REAL,                           -- data_type = num 时用
    value_text  TEXT NOT NULL DEFAULT ''        -- data_type = text 时用
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_part_param ON part_param (part_id, template_id);
-- 支撑"阻值 10k ~ 100k"这类范围筛选
CREATE INDEX IF NOT EXISTS ix_part_param_num ON part_param (template_id, value_num);

-- ---------------------------------------------------------------------------
-- 存放位置树：房间 → 柜子 → 抽屉 → 格子
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS location (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    code       TEXT    NOT NULL DEFAULT '',   -- 短码，如 A3-D2，用于贴标签
    parent_id  INTEGER REFERENCES location(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    note       TEXT    NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_location_name
    ON location (IFNULL(parent_id, 0), name);
-- 部分索引：只约束非空短码，空串可以重复（还没编号的位置）
CREATE UNIQUE INDEX IF NOT EXISTS ux_location_code
    ON location (code) WHERE code <> '';
CREATE INDEX IF NOT EXISTS ix_location_parent ON location (parent_id);

-- ---------------------------------------------------------------------------
-- 库存批次：真正记录"有多少、在哪、多少钱、什么时候买的"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_lot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id       INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
    location_id   INTEGER REFERENCES location(id) ON DELETE SET NULL,
    quantity      INTEGER NOT NULL DEFAULT 0,
    unit_price    REAL    NOT NULL DEFAULT 0,
    currency      TEXT    NOT NULL DEFAULT 'CNY',
    purchase_date TEXT    NOT NULL DEFAULT '',  -- YYYY-MM-DD
    supplier      TEXT    NOT NULL DEFAULT '',
    -- 厂商也记在批次上：同一个 10kΩ 电阻，YAGEO 买的和厚声买的是同一条器件记录，
    -- 只是两条采购批次。见 migrations.py 里 v2 的说明。
    manufacturer  TEXT    NOT NULL DEFAULT '',
    order_no      TEXT    NOT NULL DEFAULT '',
    batch_no      TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_lot_part     ON stock_lot (part_id);
CREATE INDEX IF NOT EXISTS ix_lot_location ON stock_lot (location_id);

-- ---------------------------------------------------------------------------
-- 库存流水：每一次出入库
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id    INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
    lot_id     INTEGER REFERENCES stock_lot(id) ON DELETE SET NULL,
    delta      INTEGER NOT NULL,             -- 正数入库、负数出库
    reason     TEXT    NOT NULL DEFAULT '',  -- 入库 / 出库 / 盘点 / 报废
    ref        TEXT    NOT NULL DEFAULT '',  -- 关联项目或备注
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_log_part ON stock_log (part_id, created_at);
CREATE INDEX IF NOT EXISTS ix_log_time ON stock_log (created_at);

-- ---------------------------------------------------------------------------
-- 聚合视图：列表页一次查询即可拿到总库存 / 批次数 / 最近采购 / 均价
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_part_overview;
CREATE VIEW v_part_overview AS
SELECT
    p.id,
    p.name,
    p.category_id,
    p.mpn,
    p.footprint,
    p.description,
    p.keywords,
    p.search_text,
    p.min_stock,
    p.is_active,
    COALESCE(SUM(l.quantity), 0)       AS total_qty,
    COUNT(l.id)                        AS lot_count,
    COALESCE(MAX(l.purchase_date), '') AS last_purchase,
    CASE WHEN COALESCE(SUM(l.quantity), 0) > 0
         THEN SUM(l.quantity * l.unit_price) / SUM(l.quantity)
         ELSE 0 END                    AS avg_price,
    -- 厂商与供应商是**批次**属性，这里聚合成 "YAGEO,厚声" 供列表和详情显示
    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(l.manufacturer, '')), '') AS manufacturers,
    COALESCE(GROUP_CONCAT(DISTINCT NULLIF(l.supplier, '')), '')     AS suppliers
FROM part p
LEFT JOIN stock_lot l ON l.part_id = p.id
GROUP BY p.id;
