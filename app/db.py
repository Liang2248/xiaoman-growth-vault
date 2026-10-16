"""SQLite 数据层：连接管理、schema、FTS5 全文索引、设置存取。

本地单用户应用：单连接 + 线程锁，开启 WAL 与外键。
所有日期过滤统一使用 substr(occurred_at, 1, 10)（本地时区的"日"语义），
避免 SQLite date() 函数把带偏移的时间转成 UTC 导致日期偏移。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import logx

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
THUMBS_DIR = DATA_DIR / "thumbs"
BACKUPS_DIR = DATA_DIR / "backups"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "vault.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'life',
  tags TEXT NOT NULL DEFAULT '[]',
  location_name TEXT NOT NULL DEFAULT '',
  latitude REAL,
  longitude REAL,
  weather_json TEXT,
  blocks_json TEXT,
  mood_score INTEGER,
  mood_label TEXT,
  starred INTEGER NOT NULL DEFAULT 0,
  exclude_from_ai INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_occurred ON entries(occurred_at);
-- A4-07：列表/统计统一写成 deleted_at IS NULL AND substr(occurred_at,1,10) 过滤，
-- 只建在 occurred_at 上的索引被 substr() 包住后用不上，这里补一条与查询形态一致的索引。
CREATE INDEX IF NOT EXISTS idx_entries_deleted ON entries(deleted_at);
CREATE INDEX IF NOT EXISTS idx_entries_day ON entries(deleted_at, substr(occurred_at,1,10));

CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  sha256 TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT '',
  filename TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  width INTEGER,
  height INTEGER,
  duration_ms INTEGER,
  kind TEXT NOT NULL DEFAULT 'file',
  path TEXT NOT NULL,
  thumb TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_entry ON attachments(entry_id);
CREATE INDEX IF NOT EXISTS idx_attachments_sha ON attachments(sha256);

CREATE TABLE IF NOT EXISTS links (
  id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_entry ON links(entry_id);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- 多供应商 AI 配置。API Key 仅在本机数据库保存，接口响应只返回 has_key。
CREATE TABLE IF NOT EXISTS ai_providers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  slot TEXT NOT NULL DEFAULT 'strong',
  base_url TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  vision INTEGER NOT NULL DEFAULT 0,
  effort TEXT NOT NULL DEFAULT 'auto',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_providers_slot ON ai_providers(slot, enabled, updated_at DESC);

CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  analysis_json TEXT NOT NULL DEFAULT '{}',
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  source_hash TEXT NOT NULL DEFAULT '',
  entry_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);

CREATE TABLE IF NOT EXISTS knowledge (
  id TEXT PRIMARY KEY,
  entry_id TEXT,
  source_report_id TEXT,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  type TEXT NOT NULL DEFAULT 'experience',
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  dedupe_key TEXT,
  vector_json TEXT,
  created_at TEXT NOT NULL
);
-- A4-07：stats._knowledge_counts 固定按 status='accepted' GROUP BY type 聚合。
CREATE INDEX IF NOT EXISTS idx_knowledge_status_type ON knowledge(status, type);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  current_role TEXT NOT NULL DEFAULT '',
  target_role TEXT NOT NULL DEFAULT '',
  goal_6m TEXT NOT NULL DEFAULT '',
  goal_12m TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS roadmap_nodes (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  horizon_days INTEGER NOT NULL DEFAULT 30,
  status TEXT NOT NULL DEFAULT 'suggested',
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  first_step TEXT NOT NULL DEFAULT '',
  done_when TEXT NOT NULL DEFAULT '',
  progress INTEGER NOT NULL DEFAULT 0,
  last_checkin_at TEXT,
  checkin_note TEXT NOT NULL DEFAULT '',
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS roadmap_checkins (
  id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES roadmap_nodes(id) ON DELETE CASCADE,
  progress INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecasts (
  id TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL,
  data_weeks INTEGER NOT NULL DEFAULT 0,
  stats_json TEXT NOT NULL DEFAULT '{}',
  scenarios_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS growth_summaries (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  analysis_json TEXT NOT NULL DEFAULT '{}',
  entry_count INTEGER NOT NULL DEFAULT 0,
  source_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS self_experiments (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  metric TEXT NOT NULL DEFAULT '',
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  conclusion TEXT NOT NULL DEFAULT 'undecided',
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_self_experiments_status
  ON self_experiments(status, end_date DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS capsules (
  id TEXT PRIMARY KEY,
  content TEXT NOT NULL DEFAULT '',
  unlock_date TEXT NOT NULL,
  created_at TEXT NOT NULL,
  dismissed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_capsules_unlock ON capsules(unlock_date);
"""

# FTS5 外部内容表：列名必须与 entries 的实际列一一对应（tags，不是 tags_text）。
# HANA-01：旧版本这里写成 tags_text，而 entries 里没有该列 —— FTS5 只校验列名个数，
# 建表能成功，但 count(*)/列读取/rebuild 都会报 "no such column: T.tags_text"。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  title, summary, content, tags,
  content='entries', content_rowid='rowid',
  tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS entries_fts_i AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, title, summary, content, tags)
  VALUES (new.rowid, new.title, new.summary, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_d AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, summary, content, tags)
  VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_u AFTER UPDATE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, summary, content, tags)
  VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.tags);
  INSERT INTO entries_fts(rowid, title, summary, content, tags)
  VALUES (new.rowid, new.title, new.summary, new.content, new.tags);
END;
"""

# 重建虚表前必须先删掉的三个触发器（DROP TABLE 不会连带删触发器）。
_FTS_TRIGGERS = ("entries_fts_i", "entries_fts_d", "entries_fts_u")
# 已重建到修正版结构的标记，存 settings，避免每次启动重复检测。
FTS_SCHEMA_VERSION = "2"
_FTS_VERSION_KEY = "fts_schema_version"
# A4-11：时区迁移指纹（存的是当时的本地时区名），相同则跳过全表重写。
_TZ_MIGRATED_KEY = "timezone_migrated_tz"
# FTS 自愈"已判定无效"标记：避免重建后仍不生效时每次启动都白重建一次。
_FTS_HEAL_BLOCKED_KEY = "fts_self_heal_blocked"
# A4-04：schema 版本锚点（PRAGMA user_version）。迁移本身仍靠逐列 PRAGMA 探测（幂等），
# 这个版本号用于给未来"只跑一次"的迁移提供判断依据，并让备份能记录当前结构版本。
SCHEMA_VERSION = 3

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_locked_depth = 0  # locked() 临界区嵌套深度，用于阻止块内 db.execute() 提前提交（A4-03）
_fts_repaired_this_session = False  # FTS 自愈每进程只做一次，避免每次空搜索都重建
fts_enabled = False


def knowledge_dedupe_key(title: str) -> str:
    """知识条目去重键：去首尾空白、去全部空白字符、转小写。"""
    return re.sub(r"\s+", "", title or "").lower()


def _migrate() -> None:
    """老库平滑升级：逐列检查，缺啥补啥。新库 CREATE TABLE 已带全量列。"""
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(entries)").fetchall()}
    for col, ddl in (
        ("blocks_json", "ALTER TABLE entries ADD COLUMN blocks_json TEXT"),
        ("mood_score", "ALTER TABLE entries ADD COLUMN mood_score INTEGER"),
        ("mood_label", "ALTER TABLE entries ADD COLUMN mood_label TEXT"),
        ("starred", "ALTER TABLE entries ADD COLUMN starred INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in cols:
            _conn.execute(ddl)
            cols.add(col)
    _conn.commit()
    kcols = {r[1] for r in _conn.execute("PRAGMA table_info(knowledge)").fetchall()}
    # 来源链路：知识条目可回到生成它的报告及经过校验的原始记录证据。
    for col, ddl in (
        ("source_report_id", "ALTER TABLE knowledge ADD COLUMN source_report_id TEXT"),
        ("evidence_json", "ALTER TABLE knowledge ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'"),
    ):
        if col not in kcols:
            _conn.execute(ddl)
            kcols.add(col)
            _conn.commit()
    # 旧版生成流程总是先插入知识、紧接着保存报告。仅用这个高置信时间窗口
    # 恢复报告级来源；不能证明具体来自哪条记录，因此绝不猜 entry_id。
    # 这里用 Python 比 SQLite 相关 UPDATE 更兼容旧版本 SQLite。
    try:
        reports = _conn.execute("SELECT id, created_at FROM reports").fetchall()
        legacy_knowledge = _conn.execute(
            "SELECT id, created_at FROM knowledge WHERE source_report_id IS NULL"
        ).fetchall()
        parsed_reports = []
        for report in reports:
            try:
                parsed_reports.append((report["id"], datetime.fromisoformat(report["created_at"])))
            except (TypeError, ValueError):
                continue
        for item in legacy_knowledge:
            try:
                kt = datetime.fromisoformat(item["created_at"])
            except (TypeError, ValueError):
                continue
            candidates = []
            for rid, rt in parsed_reports:
                try:
                    delta = (rt - kt).total_seconds()
                except TypeError:
                    # aware/naive 时间戳不能直接比较，宁可不回填也不猜。
                    continue
                if 0 <= delta <= 600:
                    candidates.append((delta, rid))
            if candidates:
                _conn.execute(
                    "UPDATE knowledge SET source_report_id=? WHERE id=?",
                    (min(candidates)[1], item["id"]),
                )
        _conn.commit()
    except (sqlite3.Error, TypeError, ValueError):
        # 来源回填是增强项，不能阻止应用启动。
        pass
    if "dedupe_key" not in kcols:
        _conn.execute("ALTER TABLE knowledge ADD COLUMN dedupe_key TEXT")
        # 回填存量行：dedupe_key = 规范化标题
        rows = _conn.execute("SELECT id, title FROM knowledge").fetchall()
        for row in rows:
            _conn.execute("UPDATE knowledge SET dedupe_key=? WHERE id=?",
                          (knowledge_dedupe_key(row[1]), row[0]))
        kcols.add("dedupe_key")
        _conn.commit()
    # 索引放迁移里建：老库此时才有 dedupe_key 列（放 SCHEMA 会在老库上报错）
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_dedupe ON knowledge(dedupe_key)")
    _conn.commit()
    if "vector_json" not in kcols:
        _conn.execute("ALTER TABLE knowledge ADD COLUMN vector_json TEXT")
        kcols.add("vector_json")
        _conn.commit()
    # 记录向量表（语义检索用）
    _conn.execute("""
CREATE TABLE IF NOT EXISTS entry_embeddings (
  entry_id TEXT PRIMARY KEY,
  model TEXT NOT NULL DEFAULT '',
  vector_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT ''
)""")
    _conn.commit()
    # 圈子：实体档案 / 提及证据 / 关系网
    _conn.execute("""
CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL DEFAULT 'person',
  name TEXT NOT NULL DEFAULT '',
  aliases_json TEXT NOT NULL DEFAULT '[]',
  profile TEXT NOT NULL DEFAULT '',
  relation_to_user TEXT NOT NULL DEFAULT '',
  user_note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  confidence REAL NOT NULL DEFAULT 0,
  mention_count INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT,
  last_seen TEXT,
  synthesized_at TEXT,
  needs_review INTEGER NOT NULL DEFAULT 0,
  conflict_note TEXT,
  vector_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""")
    _conn.execute("""
CREATE TABLE IF NOT EXISTS entity_mentions (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  entry_id TEXT NOT NULL,
  snippet TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
)""")
    _conn.execute("""
CREATE TABLE IF NOT EXISTS entity_relations (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  target_id TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  entry_id TEXT,
  snippet TEXT NOT NULL DEFAULT '',
  valid_from TEXT NOT NULL DEFAULT '',
  valid_to TEXT,
  certainty TEXT NOT NULL DEFAULT 'inferred',
  status TEXT NOT NULL DEFAULT 'draft',
  confidence REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)""")
    _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_type_name ON entities(type, name)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_mentions_entity ON entity_mentions(entity_id)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_source ON entity_relations(source_id)")
    # 关系也要保留时序：旧库逐列补齐，避免部分迁移失败时重复 ALTER。
    rcols = {r[1] for r in _conn.execute("PRAGMA table_info(entity_relations)").fetchall()}
    for col, ddl in (
        ("snippet", "ALTER TABLE entity_relations ADD COLUMN snippet TEXT NOT NULL DEFAULT ''"),
        ("valid_from", "ALTER TABLE entity_relations ADD COLUMN valid_from TEXT NOT NULL DEFAULT ''"),
        ("valid_to", "ALTER TABLE entity_relations ADD COLUMN valid_to TEXT"),
        ("certainty", "ALTER TABLE entity_relations ADD COLUMN certainty TEXT NOT NULL DEFAULT 'inferred'"),
    ):
        if col not in rcols:
            _conn.execute(ddl)
            rcols.add(col)
    # 存量边没有明确起始日时，以首次写入日作为保守下界；绝不伪造结束日。
    _conn.execute(
        "UPDATE entity_relations SET valid_from=substr(created_at,1,10) "
        "WHERE (valid_from IS NULL OR trim(valid_from)='') AND created_at IS NOT NULL"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_relations_current "
        "ON entity_relations(source_id, target_id, label, status, valid_to)"
    )
    _conn.commit()
    # 老库补列：冲突复核标记
    ecols = {r[1] for r in _conn.execute("PRAGMA table_info(entities)").fetchall()}
    for col, ddl in (
        ("needs_review", "ALTER TABLE entities ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0"),
        ("conflict_note", "ALTER TABLE entities ADD COLUMN conflict_note TEXT"),
        ("vector_json", "ALTER TABLE entities ADD COLUMN vector_json TEXT"),
    ):
        if col not in ecols:
            _conn.execute(ddl)
            ecols.add(col)
    _conn.commit()
    # 用户对自动确认点过「不对」的实体：永不再自动确认（只能用户手动确认）
    if "auto_confirm_blocked" not in ecols:
        _conn.execute("ALTER TABLE entities ADD COLUMN auto_confirm_blocked INTEGER NOT NULL DEFAULT 0")
        ecols.add("auto_confirm_blocked")
        _conn.commit()
    # 相关性洞察（G1）：dismiss 的 feature 永不再出
    _conn.execute("""
CREATE TABLE IF NOT EXISTS insights (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT '',
  feature TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  score REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  dismissed_at TEXT
)""")
    # 晨间意图（G2）：一天一条
    _conn.execute("""
CREATE TABLE IF NOT EXISTS daily_intents (
  day TEXT PRIMARY KEY,
  text TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
)""")
    # 年度故事卡缓存（G6）
    _conn.execute("""
CREATE TABLE IF NOT EXISTS year_reviews (
  year INTEGER PRIMARY KEY,
  payload_json TEXT NOT NULL DEFAULT '{}',
  generated_at TEXT NOT NULL
)""")
    _conn.commit()
    # 附件 AI 内容描述（G3：入嵌入索引；存量不追）
    acols = {r[1] for r in _conn.execute("PRAGMA table_info(attachments)").fetchall()}
    if "caption" not in acols:
        _conn.execute("ALTER TABLE attachments ADD COLUMN caption TEXT")
        _conn.commit()
    # A4-09/A4-37：同一条记录内同 sha256 只允许一行（物理文件本就按 sha256 去重），
    # 唯一索引让"多文件上传中途失败后整体重试"变幂等，而不是插出重复附件行。
    # 先清掉存量重复行（保留最早一行，即 rowid 最小者），否则 CREATE UNIQUE INDEX 会失败。
    try:
        dup = _conn.execute(
            "SELECT count(*) FROM attachments WHERE rowid NOT IN "
            "(SELECT min(rowid) FROM attachments GROUP BY entry_id, sha256)").fetchone()[0]
        if dup:
            _conn.execute(
                "DELETE FROM attachments WHERE rowid NOT IN "
                "(SELECT min(rowid) FROM attachments GROUP BY entry_id, sha256)")
            logx.log("已合并重复的附件行", f"{dup} 行为同一记录内重复引用同一文件（保留最早一行）")
        _conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_entry_sha "
            "ON attachments(entry_id, sha256)")
    except sqlite3.Error as e:
        # 老库可能有异常数据；唯一约束是增强项，失败不阻止启动。
        logx.log("附件唯一约束创建失败", str(e))
    _conn.commit()
    # 合并提案 / 主动提问
    _conn.execute("""
CREATE TABLE IF NOT EXISTS merge_proposals (
  id TEXT PRIMARY KEY,
  from_id TEXT NOT NULL,
  into_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
)""")
    _conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposals_pending "
        "ON merge_proposals(from_id, into_id) WHERE status='pending'")
    _conn.execute("""
CREATE TABLE IF NOT EXISTS circle_questions (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL DEFAULT '',
  options_json TEXT NOT NULL DEFAULT '[]',
  related_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  answer TEXT,
  created_at TEXT NOT NULL
)""")
    _conn.commit()
    # 老库补列：提问归属记录与 AI 预选答案下标（批量提问卡用）
    qcols = {r[1] for r in _conn.execute("PRAGMA table_info(circle_questions)").fetchall()}
    for col, ddl in (
        ("entry_id", "ALTER TABLE circle_questions ADD COLUMN entry_id TEXT"),
        ("ai_suggested", "ALTER TABLE circle_questions ADD COLUMN ai_suggested INTEGER"),
    ):
        if col not in qcols:
            _conn.execute(ddl)
            qcols.add(col)
    _conn.commit()
    # 分类体系 v2：is_work 硬标签 + AI 多标签 + 隐性关联标记（逐列独立检查，防部分列残留）
    for col, ddl in (
        ("is_work", "ALTER TABLE entries ADD COLUMN is_work INTEGER"),
        ("is_work_manual", "ALTER TABLE entries ADD COLUMN is_work_manual INTEGER NOT NULL DEFAULT 0"),
        ("work_related", "ALTER TABLE entries ADD COLUMN work_related INTEGER NOT NULL DEFAULT 0"),
        ("link_checked", "ALTER TABLE entries ADD COLUMN link_checked INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in cols:
            _conn.execute(ddl)
            cols.add(col)
            _conn.commit()
    # 类目体系：12 预设类目（source='preset'），月度聚类可新增 AI 类目（pending 待审）
    _conn.execute("""
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  name_zh TEXT NOT NULL DEFAULT '',
  name_en TEXT NOT NULL DEFAULT '',
  definition TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'preset',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
)""")
    _conn.execute("""
CREATE TABLE IF NOT EXISTS entry_categories (
  entry_id TEXT NOT NULL,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  confidence REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'ai',
  PRIMARY KEY(entry_id, category_id)
)""")
    # 时序事实：旧值 valid_to 封存不覆盖（学 Zep 双时态）
    _conn.execute("""
CREATE TABLE IF NOT EXISTS entity_facts (
  id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  predicate TEXT NOT NULL DEFAULT '',
  object_text TEXT NOT NULL DEFAULT '',
  valid_from TEXT NOT NULL DEFAULT '',
  valid_to TEXT,
  source_entry_id TEXT,
  certainty TEXT NOT NULL DEFAULT 'inferred',
  created_at TEXT NOT NULL
)""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_entity ON entity_facts(entity_id)")
    # A0-24：entity_facts 也要有与 entity_relations 同款的逐列补列。
    # 早于 valid_to/source_entry_id/certainty 引入版本的库只有 CREATE TABLE IF NOT EXISTS，
    # 不会补上这些列，而 temporal.py 会直接索引它们（sqlite3.Row 缺列抛 IndexError）。
    fcols = {r[1] for r in _conn.execute("PRAGMA table_info(entity_facts)").fetchall()}
    for col, ddl in (
        ("valid_from", "ALTER TABLE entity_facts ADD COLUMN valid_from TEXT NOT NULL DEFAULT ''"),
        ("valid_to", "ALTER TABLE entity_facts ADD COLUMN valid_to TEXT"),
        ("certainty", "ALTER TABLE entity_facts ADD COLUMN certainty TEXT NOT NULL DEFAULT 'inferred'"),
        ("source_entry_id", "ALTER TABLE entity_facts ADD COLUMN source_entry_id TEXT"),
    ):
        if col not in fcols:
            _conn.execute(ddl)
            fcols.add(col)
    _conn.commit()
    # 成长路线证据与验收条件（旧库增量补列，保留已有节点）
    rcols = {r[1] for r in _conn.execute("PRAGMA table_info(roadmap_nodes)").fetchall()}
    for col, ddl in (
        ("evidence_json", "ALTER TABLE roadmap_nodes ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'"),
        ("first_step", "ALTER TABLE roadmap_nodes ADD COLUMN first_step TEXT NOT NULL DEFAULT ''"),
        ("done_when", "ALTER TABLE roadmap_nodes ADD COLUMN done_when TEXT NOT NULL DEFAULT ''"),
        ("progress", "ALTER TABLE roadmap_nodes ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"),
        ("last_checkin_at", "ALTER TABLE roadmap_nodes ADD COLUMN last_checkin_at TEXT"),
        ("checkin_note", "ALTER TABLE roadmap_nodes ADD COLUMN checkin_note TEXT NOT NULL DEFAULT ''"),
        ("completed_at", "ALTER TABLE roadmap_nodes ADD COLUMN completed_at TEXT"),
    ):
        if col not in rcols:
            _conn.execute(ddl)
            rcols.add(col)  # A4-14：与其他迁移循环保持一致，防重复 ALTER
    _conn.commit()
    _conn.execute("""
CREATE TABLE IF NOT EXISTS roadmap_checkins (
  id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES roadmap_nodes(id) ON DELETE CASCADE,
  progress INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
)""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_roadmap_checkins_node ON roadmap_checkins(node_id, created_at DESC)")
    # 用户拒绝过的合并配对黑名单（id 对排序后存储），永不再提
    _conn.execute("""
CREATE TABLE IF NOT EXISTS rejected_pairs (
  a_id TEXT NOT NULL,
  b_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(a_id, b_id)
)""")
    # 隐性关联提案：一条记录对同一目标只提一次
    _conn.execute("""
CREATE TABLE IF NOT EXISTS link_proposals (
  id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  target_type TEXT NOT NULL DEFAULT 'entry',
  target_id TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE(entry_id, target_type, target_id)
)""")
    # A4-07：links_for_entry / circle 聚合固定按 (entry_id, status) 过滤提案。
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_link_proposals_entry "
                  "ON link_proposals(entry_id, status)")
    # 一切 AI 自动动作的可逆日志（操作前快照存 detail_json，undone=1 表示已撤销）
    _conn.execute("""
CREATE TABLE IF NOT EXISTS circle_auto_log (
  id TEXT PRIMARY KEY,
  action TEXT NOT NULL DEFAULT 'confirm',
  detail_json TEXT NOT NULL DEFAULT '{}',
  undone INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
)""")
    _conn.commit()


def ensure_dirs() -> None:
    for d in (DATA_DIR, MEDIA_DIR, THUMBS_DIR, BACKUPS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


# 12 预设类目（slug, 中文名, 英文名, 定义）——AI 打标与用户筛选共用这一份
PRESET_CATEGORIES = [
    ("work_tasks", "工作事务", "work tasks", "实习/工作/求职里的日常任务、会议、沟通、流程事务"),
    ("field_farm", "现场与农事", "field & farm", "田间、大棚、基地等现场作业与农事操作"),
    ("problem_solving", "问题解决", "problem solving", "排查、调试、攻克某个具体问题或故障的过程"),
    ("learning", "学习成长", "learning", "读书、课程、练习、复盘、技能精进"),
    ("people", "人际协作", "people & collaboration", "与同事、领导、客户、合作方的沟通协作与人情往来"),
    ("ideas", "想法灵感", "ideas", "闪念、点子、计划、对未来的设想"),
    ("feelings", "心情感受", "feelings", "情绪、心境、自我感受的记录"),
    ("health", "身体与健康", "health", "睡眠、运动、饮食、看病、身体状态"),
    ("daily_life", "生活日常", "daily life", "柴米油盐、通勤、家务、日常琐事"),
    ("family_friends", "亲友时光", "family & friends", "与家人、恋人、朋友相处的时间"),
    ("hobbies", "兴趣娱乐", "hobbies", "游戏、影视、音乐、旅行、兴趣爱好"),
    ("money_things", "钱与物", "money & things", "收支、购物、物品置办与维修"),
]


def seed_categories() -> None:
    """预设类目播种：INSERT OR IGNORE，幂等，每次启动跑。"""
    with _lock:
        for slug, name_zh, name_en, definition in PRESET_CATEGORIES:
            _conn.execute(
                "INSERT OR IGNORE INTO categories(slug, name_zh, name_en, definition, "
                "source, status, created_at) VALUES(?,?,?,?, 'preset', 'active', ?)",
                (slug, name_zh, name_en, definition, now_iso()),
            )
        _conn.commit()


def init_db() -> None:
    """启动时调用：建目录、建表、尝试创建 FTS。"""
    global _conn, fts_enabled
    ensure_dirs()
    self_check_warning = ""
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        # A4-05：默认 busy_timeout 为 0，拿不到锁立刻 FAIL；给 10 秒重试窗口，
        # 与 connect(timeout=10) 等价，双实例/外部工具短暂占用不再直接 500。
        _conn.execute("PRAGMA busy_timeout=10000")
        _conn.execute("PRAGMA foreign_keys=ON")
        # A4-04：先确认这确实是个能打开的 SQLite 库。不是的话（被别的程序写坏、
        # 根本不是库文件）给出中文原因，而不是让 executescript 抛英文栈。
        try:
            chk = _conn.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise RuntimeError(
                f"数据库文件无法打开或已损坏（{DB_PATH}）：{str(e)[:200]}。"
                f"请关闭小满后，用 data/backups 下最近的备份 zip 里的 vault.db 覆盖恢复。"
            ) from e
        if chk != "ok":
            # 结构性问题只告警不阻断：数据保全应用必须还能启动起来导出/备份现有数据。
            self_check_warning = str(chk)[:200]
            logx.log("数据库自检未通过", f"{self_check_warning}（仍继续启动，建议尽快全量备份）")
        _conn.executescript(SCHEMA)
        _migrate()
        ensure_ai_provider_defaults()
        if self_check_warning:
            # 放到建表之后：此刻 settings 表一定存在，提示才能落库
            push_notice("数据库自检发现异常，建议先在设置页做一次完整备份")
        # A4-04：记录 schema 版本，给后续迁移一个显式的版本锚点
        try:
            if _conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                _conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.Error:
            pass
        try:
            _ensure_fts()
            fts_enabled = True
        except sqlite3.Error as e:
            # A4-12：降级原因必须可见（缺 FTS5、SQLite < 3.34 无 trigram 等）
            fts_enabled = False
            logx.log("全文检索降级", f"FTS5 不可用，3 字以上查询改走 LIKE：{e}")
        _conn.commit()
    seed_categories()
    _migrate_timezones()
    if fts_enabled:
        _fts_self_heal()


def _ensure_fts() -> None:
    """建/修 entries_fts 并保证索引与 entries 一致（幂等，可重跑）。

    HANA-01：旧库的 entries_fts 用错列名 tags_text 建过，FTS5 建表不校验列名存在性，
    于是 count(*)/列读取/rebuild 全报 "no such column: T.tags_text"，只有 MATCH 碰巧能用。
    这里读 sqlite_master 的定义 SQL 做检测，是旧结构（或缺失）就整体重建 + rebuild。
    """
    sql = _conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries_fts'"
    ).fetchone()
    ddl = (sql["sql"] if sql else "") or ""
    # 未记录过版本（全新库 / 老库）或 DDL 里还带着错误的 tags_text 列名 → 重建。
    # 用 settings 里的版本标记是为了让"已经修过的库"每次启动都跳过这段检测。
    flagged = get_setting(_FTS_VERSION_KEY) != FTS_SCHEMA_VERSION
    stale = ("tags_text" in ddl) or (not ddl) or flagged
    if stale:
        for name in _FTS_TRIGGERS:
            _conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        _conn.execute("DROP TABLE IF EXISTS entries_fts")
        _conn.commit()
    _conn.executescript(FTS_SCHEMA)
    if stale:
        # 重建后索引是空的：外部内容表必须显式 rebuild 才会把存量读进来。
        _conn.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
        set_setting(_FTS_VERSION_KEY, FTS_SCHEMA_VERSION)
        _conn.commit()
        logx.log("全文索引已重建",
                 f"结构校对完成，共 {_conn.execute('SELECT count(*) FROM entries_fts').fetchone()[0]} 条已索引")


def _fts_canary_miss() -> bool:
    """独立于 count(*) 语义的索引覆盖探测：取一条真实记录里的短片段去 MATCH。

    为什么不能用 count 比对：`entries_fts` 是 `content='entries'` 的外部内容表，
    其上的 `SELECT count(*) FROM entries_fts` 会**转去读内容表**（HANA-01 生产实测报
    `no such column: T.tags_text` 正是这个原因），因此它恒等于 `SELECT count(*) FROM entries`，
    完全无法反映索引是否覆盖了存量数据。这里改用"金丝雀"：从最新一条记录的某个字段里
    取 3 个非空白非引号字符组成短语去 MATCH，搜不到就说明该记录没进索引。
    取不到可用探针时返回 False（无法判定，不重建）。
    """
    row = _conn.execute(
        "SELECT rowid, title, summary, content, tags FROM entries WHERE rowid = "
        "(SELECT max(rowid) FROM entries)").fetchone()
    if row is None:
        return False  # 空库：无从探测
    probed = False
    for col in ("title", "summary", "content", "tags"):
        m = re.search(r'[^\s"]{3,}', (row[col] or ""))
        if not m:
            continue
        probed = True
        phrase = '"' + m.group(0)[:3] + '"'
        try:
            hit = _conn.execute(
                "SELECT 1 FROM entries_fts WHERE entries_fts MATCH ? LIMIT 1",
                (phrase,)).fetchone()
        except sqlite3.Error:
            return True  # MATCH 报错 = 索引不可用
        if hit is not None:
            return False  # 这条记录已在索引里，索引可用
    return probed  # 全部可用探针都搜不到 → 漏索引；没有探针 → 不判定


def _fts_integrity_check() -> str:
    """跑 FTS5 的 integrity-check；返回 "" 表示通过，否则返回中文原因。

    先试带第二参数的形式，再回退单参数形式。两者的差别（是否连带比对外部内容表）
    依 SQLite 版本而不同，故写成"能用哪个用哪个"：任一形式报错都视作未通过。
    """
    last = ""
    for sql in ("INSERT INTO entries_fts(entries_fts, rank) VALUES('integrity-check', 1)",
                "INSERT INTO entries_fts(entries_fts) VALUES('integrity-check')"):
        try:
            _conn.execute(sql)
            return ""
        except sqlite3.Error as e:
            last = str(e)
    return f"integrity-check 未通过：{last}"


def _fts_self_heal() -> None:
    """A4-01/B + A4-13：启动时自检 FTS 索引，发现不一致就 rebuild（幂等、低成本）。

    两个探测信号（都**不**依赖外部内容表的 count(*) 语义）：
    1. 金丝雀 MATCH（`_fts_canary_miss`）—— 索引没覆盖存量记录时必然命中失败；
    2. FTS5 官方 `integrity-check`（`_fts_integrity_check`）—— 唯一能发现
       "影子表与索引不同步"的手段（`PRAGMA integrity_check` 不覆盖 FTS5 虚表）。

    另外把 `count(*) FROM entries_fts` 与 `count(*) FROM entries` 的差值只作为**日志信息**
    记录（两者对外部内容表本就应当相等，不构成判据）。
    """
    with _lock:
        try:
            if get_setting(_FTS_HEAL_BLOCKED_KEY):
                return  # 上次判定自动重建无效，不再每次启动白重建
            try:
                indexed = _conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0]
            except sqlite3.Error:
                indexed = -1
            live = _conn.execute("SELECT count(*) FROM entries").fetchone()[0]
            broken = False
            reason = ""
            if live and _fts_canary_miss():
                broken, reason = True, "金丝雀匹配失败：索引未覆盖存量记录"
            if not broken:
                err = _fts_integrity_check()
                if err:
                    broken, reason = True, err
            if broken:
                _conn.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
                _conn.commit()
                after = _conn.execute("SELECT count(*) FROM entries_fts").fetchone()[0]
                # 重建后复核一次：若金丝雀仍失败，说明不是"索引没建"而是别的原因，
                # 打上标记后不再每次启动都重建（避免陷入每启动一次重建的死循环）。
                if live and _fts_canary_miss():
                    set_setting(_FTS_HEAL_BLOCKED_KEY, "1")
                    logx.log("全文索引自愈未生效", "重建后仍无法覆盖存量记录，已停止自动重建；"
                                                    "搜索仍走 LIKE 兜底，可在诊断页查看")
                else:
                    set_setting(_FTS_HEAL_BLOCKED_KEY, "")
                    logx.log("全文索引已自愈", f"{reason}，已重建（索引 {after} 条 / 记录 {live} 条）")
            elif indexed != live:
                # 外部内容表下两者本应相等；不等只记日志，不据此重建（避免误判）
                logx.log("全文索引条数与记录数不一致", f"索引 {indexed} 条 / 记录 {live} 条（仅记录，不据此重建）")
        except sqlite3.Error as e:
            # 自愈失败不影响启动：search_rowids 仍有 LIKE 兜底。
            logx.log("全文索引自检未通过", f"{e}")


def fts_status() -> dict:
    """A4-12/A4-13：把全文索引状态暴露给诊断接口（设置页/健康检查用）。

    注意 `indexed`（`count(*) FROM entries_fts`）在外部内容表下等于记录数，
    不具备诊断价值，这里同时给出真正有意义的 `canary_ok` 与 `integrity` 结论。
    """
    if not fts_enabled:
        return {"enabled": False, "detail": "FTS5 不可用，3 字以上查询走 LIKE"}
    with _lock:
        try:
            live = _conn.execute("SELECT count(*) FROM entries").fetchone()[0]
            canary_ok = not (live and _fts_canary_miss())
            integrity = _fts_integrity_check()
            return {"enabled": True, "entries": live,
                    "canary_ok": canary_ok,
                    "integrity_ok": not integrity,
                    "consistent": bool(canary_ok and not integrity),
                    "detail": integrity or ("索引覆盖正常" if canary_ok else "索引未覆盖存量记录")}
        except sqlite3.Error as e:
            return {"enabled": True, "consistent": False, "detail": str(e)}


def _migrate_timezones() -> None:
    """老数据的 occurred_at 可能是 UTC 等非本地偏移（前端曾发 toISOString），
    统一转成本地时区，保证 substr(occurred_at,1,10) 的本地日聚合正确。幂等，每次启动跑。

    A4-11：全表重写最核心字段有风险，这里加时区指纹标记：时区没变且已迁移过就直接跳过，
    避免每次启动都全表扫描 + 触发 FTS 更新触发器。"""
    local_tz = datetime.now().astimezone().tzinfo
    tz_name = str(local_tz)
    with _lock:
        if get_setting(_TZ_MIGRATED_KEY) == tz_name:
            return
        rows = _conn.execute("SELECT id, occurred_at FROM entries").fetchall()
        dirty = 0
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["occurred_at"])
            except (ValueError, TypeError):
                # A4-11：无法解析的值原样保留，但记数并在启动后提示，不再完全静默。
                dirty += 1
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz)
            else:
                dt = dt.astimezone(local_tz)
            s = dt.isoformat(timespec="seconds")
            if s != r["occurred_at"]:
                _conn.execute("UPDATE entries SET occurred_at=? WHERE id=?", (s, r["id"]))
        _conn.commit()
        set_setting(_TZ_MIGRATED_KEY, tz_name)
    if dirty:
        push_notice(f"有 {dirty} 条记录的时间格式无法识别，已原样保留，可在记录里手动修正")
        logx.log("时间格式检查", f"{dirty} 条记录的 occurred_at 无法解析，已原样保留")


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("数据库尚未初始化")
    return _conn


def close() -> None:
    """A4-05：关停时收口 WAL 再关连接（lifespan 退出时调用）。

    不做 checkpoint 的话，长时间运行后 vault.db-wal 会一直偏大。"""
    global _conn
    with _lock:
        if _conn is None:
            return
        try:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            _conn.close()
        finally:
            _conn = None


def purge_orphan_rows() -> dict:
    """A4-08 + R1-10：清理引用已硬删记录的孤儿行（启动时调用，幂等）。

    knowledge/entity_mentions/entry_categories/link_proposals/circle_questions 引用 entries
    却没有外键级联（老库 SQLite 不支持 ALTER ADD CONSTRAINT），main.purge_old_deleted 硬删
    记录后它们会留下孤儿：entity_mentions 的孤儿会把 entities.mention_count 抬高，
    link_proposals 的孤儿会在关联列表里显示"（记录已删除）"，circle_questions 的孤儿提问卡
    会指向不存在的记录，entry_embeddings 的孤儿向量会继续参与语义检索。

    处置口径（保守，与 purge_old_deleted 一致）：
    - 派生于记录、无独立内容价值的行 → DELETE（entity_mentions / entry_categories /
      link_proposals / entry_embeddings）；
    - 自身有内容价值、entry_id 只是"来源指针"的 → 置 NULL，保留本体
      （knowledge.entry_id / circle_questions.entry_id / entity_facts.source_entry_id）。
    """
    counts: dict[str, int] = {}
    with locked() as c:
        # 纯派生的孤儿行：直接删
        for table in ("entity_mentions", "entry_categories", "link_proposals", "entry_embeddings"):
            # 用 NOT EXISTS 而不是 NOT IN：NOT IN 遇到子查询里的 NULL 会整体变成"无匹配"
            # （静默不清理），NOT EXISTS 语义确定。
            cur = c.execute(
                f"DELETE FROM {table} WHERE entry_id IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.id = {table}.entry_id)")
            counts[table] = max(cur.rowcount, 0)
        # 悬空"来源指针"：只置空，不删本体
        for table, col in (("knowledge", "entry_id"),
                           ("circle_questions", "entry_id"),
                           ("entity_facts", "source_entry_id")):
            cur = c.execute(
                f"UPDATE {table} SET {col}=NULL WHERE {col} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.id = {table}.{col})")
            counts[f"{table}.{col}_nulled"] = max(cur.rowcount, 0)
    if any(counts.values()):
        logx.log("已清理孤儿数据行", "、".join(f"{k} {v} 行" for k, v in counts.items() if v))
    return counts


@contextmanager
def locked():
    """跨多条语句的复合操作时用：with locked() as c: ...（持锁，**成功即提交**）。

    A4-02：事务由本上下文托管，成功路径也 commit——调用方漏写 c.commit() 不再会让
    半成品事务滞留在连接里、被另一个线程的下一次 commit 捎带落库。
    块内调用 db.execute()/db.q1() 是安全的：execute() 检测到临界区内只执行不提交
    （并入本次事务），由本上下文统一提交。中途抛异常必须 rollback。"""
    global _locked_depth
    with _lock:
        c = conn()
        nested = _locked_depth > 0
        _locked_depth += 1
        try:
            yield c
        except BaseException:
            # 块内抛异常（含生成器被外部 close 时的 GeneratorExit）一律回滚；
            # 正常退出（含块内 return）走 else 分支提交。
            c.rollback()
            raise
        else:
            if not nested:
                c.commit()
        finally:
            _locked_depth -= 1


def q(sql: str, params=()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def q1(sql: str, params=()) -> sqlite3.Row | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()) -> None:
    with _lock:
        conn().execute(sql, params)
        if _locked_depth == 0:
            conn().commit()
        else:
            # A4-03：临界区内调用不再提前提交外层事务（旧行为会把复合写入做到一半落库）。
            logx.log("数据层提示", "locked() 临界区内调用了 db.execute()，已并入外层事务")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------- 设置 ----------------

def get_setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def set_settings(values: dict[str, str]) -> None:
    """一次事务写入一组设置，供切换 AI 供应商等不可拆分操作使用。

    R1-07：不再在块内显式 c.commit()——提交由 locked() 托管，块内提交会与外层事务语义冲突。"""
    if not values:
        return
    with locked() as c:
        c.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, str(value)) for key, value in values.items()],
        )


# ---------------- 多供应商 AI 配置 ----------------

AI_PROVIDER_SLOTS = ("strong", "fast", "embed")


def ai_provider_rows() -> list[sqlite3.Row]:
    """返回所有 AI 供应商，按槽位和最近更新时间排序。"""
    return q("SELECT * FROM ai_providers ORDER BY "
             "CASE slot WHEN 'strong' THEN 0 WHEN 'fast' THEN 1 ELSE 2 END, "
             "updated_at DESC, created_at DESC")


def ai_provider_row(provider_id: str) -> sqlite3.Row | None:
    return q1("SELECT * FROM ai_providers WHERE id=?", (provider_id,))


def ai_provider_insert(*, provider_id: str, name: str, slot: str, base_url: str,
                       api_key: str, model: str, vision: bool = False,
                       effort: str = "auto", enabled: bool = True) -> None:
    now = now_iso()
    execute(
        "INSERT INTO ai_providers(id,name,slot,base_url,api_key,model,vision,effort,enabled,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (provider_id, name, slot, base_url, api_key, model, int(bool(vision)),
         effort, int(bool(enabled)), now, now),
    )


def ai_provider_update(provider_id: str, **fields) -> None:
    allowed = {"name", "slot", "base_url", "api_key", "model", "vision", "effort", "enabled"}
    updates = [(k, fields[k]) for k in fields if k in allowed]
    if not updates:
        return
    updates.append(("updated_at", now_iso()))
    sql = "UPDATE ai_providers SET " + ", ".join(f"{k}=?" for k, _ in updates) + " WHERE id=?"
    execute(sql, tuple(v for _, v in updates) + (provider_id,))


def ai_provider_delete(provider_id: str) -> None:
    execute("DELETE FROM ai_providers WHERE id=?", (provider_id,))


def ensure_ai_provider_defaults() -> None:
    """把旧版三个 KV 槽位一次性导入供应商表，保证升级后无配置丢失。"""
    legacy = {
        "strong": ("ai_base_url", "ai_key", "ai_model", "ai_vision", "ai_effort", "当前大模型"),
        "fast": ("ai_fast_base_url", "ai_fast_key", "ai_fast_model", "ai_vision", "ai_fast_effort", "当前小模型"),
        "embed": ("embed_base_url", "embed_key", "embed_model", "ai_vision", "ai_effort", "当前嵌入模型"),
    }
    for slot, (url_key, key_key, model_key, vision_key, effort_key, name) in legacy.items():
        if q1("SELECT id FROM ai_providers WHERE slot=? LIMIT 1", (slot,)):
            continue
        base_url = get_setting(url_key).strip()
        api_key = get_setting(key_key).strip()
        model = get_setting(model_key).strip()
        if not (base_url or api_key or model):
            continue
        provider_id = f"legacy-{slot}"
        ai_provider_insert(provider_id=provider_id, name=name, slot=slot,
                           base_url=base_url, api_key=api_key, model=model,
                           vision=get_setting(vision_key) == "true",
                           effort=get_setting(effort_key) or "auto")
        set_setting(f"ai_active_{slot}", provider_id)
    # 异常退出或旧测试数据可能只留下列表、没有当前项；启动时自动修复指针。
    for slot in AI_PROVIDER_SLOTS:
        active_id = get_setting(f"ai_active_{slot}")
        if active_id and ai_provider_row(active_id):
            continue
        row = q1(
            "SELECT id FROM ai_providers WHERE slot=? AND enabled=1 "
            "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
            (slot,),
        )
        set_setting(f"ai_active_{slot}", row["id"] if row else "")


def push_notice(text: str) -> None:
    """一次性轻提示队列（setting app_notice 存 JSON 数组）：多条共存不互顶，
    GET /api/notice 一次取走全部。保留最近 20 条。

    A4-10：整个读-改-写放在同一临界区（同一事务），避免两个后台线程各自基于同一份
    旧列表覆盖写、丢掉其中一条提示。"""
    with locked() as c:
        row = c.execute("SELECT value FROM settings WHERE key='app_notice'").fetchone()
        items: list = []
        if row and row["value"]:
            try:
                v = json.loads(row["value"])
                items = v if isinstance(v, list) else [v]
            except ValueError:
                items = []
        items.append({"text": text, "ts": now_iso()})
        c.execute(
            "INSERT INTO settings(key, value) VALUES('app_notice', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(items[-20:], ensure_ascii=False),),
        )


# ---------------- 全文搜索 ----------------

def _like_escape(s: str) -> str:
    """A4-06：LIKE 的参数化只防注入、不防模式注入；用户查询里的 % 与 _ 必须转义。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_rowids(qs: str, limit: int) -> list[int]:
    like = f"%{_like_escape(qs)}%"
    rows = conn().execute(
        "SELECT rowid FROM entries WHERE title LIKE ? ESCAPE '\\' "
        "OR summary LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
        "OR tags LIKE ? ESCAPE '\\' LIMIT ?",
        (like, like, like, like, limit),
    ).fetchall()
    return [r[0] for r in rows]


def search_rowids(query: str, limit: int = 500) -> list[int]:
    """返回匹配的 entries.rowid 列表。

    3 个及以上字符走 FTS5 trigram（子串匹配）；1~2 个汉字直接 LIKE
    （trigram 对短查询不可靠，LIKE 结果一定正确）；FTS 不可用时回退 LIKE。
    """
    global fts_enabled, _fts_repaired_this_session
    qs = query.strip()
    if not qs:
        return []
    with _lock:
        if fts_enabled and len(qs) >= 3:
            try:
                hits = _fts_rowids(qs, limit)
                if hits:
                    return hits
                # A4-01：空结果不等于没有匹配——索引缺失/漂移同样返回空集。
                # 先用 LIKE 拿到正确结果，确认确有匹配时再自愈索引（每进程最多一次，
                # 否则每次"搜不到"都要重建整表索引，代价不可接受）。
                rows = _like_rowids(qs, limit)
                if rows and not _fts_repaired_this_session:
                    _fts_repaired_this_session = True
                    try:
                        conn().execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")
                        conn().commit()
                        logx.log("全文索引已自愈", "FTS 无结果但 LIKE 有命中，已重建索引")
                    except sqlite3.Error as e:
                        logx.log("全文索引重建失败", f"{e}（本次仍返回 LIKE 结果）")
                return rows
            except sqlite3.Error as e:
                # 索引彻底不可用时永久降级到 LIKE，避免每次查询都白跑一次 FTS。
                fts_enabled = False
                logx.log("全文检索降级", f"FTS 查询失败，后续改走 LIKE：{e}")
        return _like_rowids(qs, limit)


def _fts_rowids(qs: str, limit: int) -> list[int]:
    phrase = '"' + qs.replace('"', '""') + '"'
    rows = conn().execute(
        "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (phrase, limit),
    ).fetchall()
    return [r[0] for r in rows]
