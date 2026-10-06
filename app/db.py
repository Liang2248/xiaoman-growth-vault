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
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  title, summary, content, tags_text,
  content='entries', content_rowid='rowid',
  tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS entries_fts_i AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, title, summary, content, tags_text)
  VALUES (new.rowid, new.title, new.summary, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_d AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, summary, content, tags_text)
  VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_u AFTER UPDATE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, summary, content, tags_text)
  VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.tags);
  INSERT INTO entries_fts(rowid, title, summary, content, tags_text)
  VALUES (new.rowid, new.title, new.summary, new.content, new.tags);
END;
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
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
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _migrate()
        ensure_ai_provider_defaults()
        try:
            _conn.executescript(FTS_SCHEMA)
            fts_enabled = True
        except sqlite3.Error:
            fts_enabled = False
        _conn.commit()
    seed_categories()
    _migrate_timezones()


def _migrate_timezones() -> None:
    """老数据的 occurred_at 可能是 UTC 等非本地偏移（前端曾发 toISOString），
    统一转成本地时区，保证 substr(occurred_at,1,10) 的本地日聚合正确。幂等，每次启动跑。"""
    local_tz = datetime.now().astimezone().tzinfo
    with _lock:
        rows = _conn.execute("SELECT id, occurred_at FROM entries").fetchall()
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["occurred_at"])
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz)
            else:
                dt = dt.astimezone(local_tz)
            s = dt.isoformat(timespec="seconds")
            if s != r["occurred_at"]:
                _conn.execute("UPDATE entries SET occurred_at=? WHERE id=?", (s, r["id"]))
        _conn.commit()


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("数据库尚未初始化")
    return _conn


@contextmanager
def locked():
    """跨多条语句的复合操作时用：with locked() as c: ...（持锁且不自动提交）。
    中途抛异常必须 rollback——否则半成品事务会被其他线程的下一次 commit 捎带落库。"""
    with _lock:
        c = conn()
        try:
            yield c
        except Exception:
            c.rollback()
            raise


def q(sql: str, params=()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def q1(sql: str, params=()) -> sqlite3.Row | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()) -> None:
    with _lock:
        conn().execute(sql, params)
        conn().commit()


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
    """一次事务写入一组设置，供切换 AI 供应商等不可拆分操作使用。"""
    if not values:
        return
    with locked() as c:
        c.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(key, str(value)) for key, value in values.items()],
        )
        c.commit()


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
    GET /api/notice 一次取走全部。保留最近 20 条。"""
    raw = get_setting("app_notice")
    items: list = []
    if raw:
        try:
            v = json.loads(raw)
            items = v if isinstance(v, list) else [v]
        except ValueError:
            items = []
    items.append({"text": text, "ts": now_iso()})
    set_setting("app_notice", json.dumps(items[-20:], ensure_ascii=False))


# ---------------- 全文搜索 ----------------

def search_rowids(query: str, limit: int = 500) -> list[int]:
    """返回匹配的 entries.rowid 列表。

    3 个及以上字符走 FTS5 trigram（子串匹配）；1~2 个汉字直接 LIKE
    （trigram 对短查询不可靠，LIKE 结果一定正确）；FTS 不可用时回退 LIKE。
    """
    qs = query.strip()
    if not qs:
        return []
    with _lock:
        if fts_enabled and len(qs) >= 3:
            try:
                phrase = '"' + qs.replace('"', '""') + '"'
                rows = conn().execute(
                    "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (phrase, limit),
                ).fetchall()
                return [r[0] for r in rows]
            except sqlite3.Error:
                pass
        like = f"%{qs}%"
        rows = conn().execute(
            "SELECT rowid FROM entries WHERE title LIKE ? OR summary LIKE ? "
            "OR content LIKE ? OR tags LIKE ? LIMIT ?",
            (like, like, like, like, limit),
        ).fetchall()
        return [r[0] for r in rows]
