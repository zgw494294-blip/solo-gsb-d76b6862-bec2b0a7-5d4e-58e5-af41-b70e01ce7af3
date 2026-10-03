"""SQLite 持久化层。

- 数据库文件路径：环境变量 PLANNER_DB > /data/planner.db（容器 Compose 卷）
  > 仓库内 data/planner.db（本地裸跑兜底）。
- 读操作只经过进程内全局读锁（_LOCK），拿到的一定是同一版本下自洽的
  参数与结果。
- 写操作一律按「写互斥锁（_WRITE_LOCK）→ 全局读锁」的顺序取锁，保证
  「配方修改」「归档」「停用」「新建炉次」按某一先后顺序整体生效。
  配方重算与新建炉次的耗时求解都在两把锁之外进行，期间读操作仍可
  进入并读到上一次完整提交的状态（新草稿在整体提交前不可见），只有
  最后的复核+提交瞬间需要短暂持锁；连续竞争导致快照复核反复失效时，
  兜底路径持写互斥锁走完 快照→求解→提交 全程——其他写操作在写互斥
  锁上排队（读操作不经过它），求解期间读取照常返回上一整次提交的
  状态。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_LOCK = threading.RLock()
# 仅写操作持有的互斥锁，读操作不经过它。兜底重算持它走完 快照→求解→提交
# 全程：其他写操作在它上面排队（保持某一先后顺序），读取则完全不受影响。
_WRITE_LOCK = threading.RLock()


def db_path() -> Path:
    env = os.environ.get("PLANNER_DB")
    if env:
        return Path(env)
    preferred = Path("/data/planner.db")
    if os.access("/", os.W_OK) or preferred.parent.exists():
        try:
            preferred.parent.mkdir(parents=True, exist_ok=True)
            return preferred
        except OSError:
            pass
    local = Path(__file__).resolve().parent.parent / "data" / "planner.db"
    local.parent.mkdir(parents=True, exist_ok=True)
    return local


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS recipes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    hold_low    INTEGER NOT NULL,
    hold_high   INTEGER NOT NULL,
    duration    INTEGER NOT NULL,
    tolerance   INTEGER NOT NULL,
    -- 'active' 可用（默认）/ 'disabled' 停用；停用后不可被新炉次引用，
    -- 已有引用草稿与已归档方案不受影响
    status      TEXT    NOT NULL DEFAULT 'active',
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    -- 按顺序引用 2~3 个不同的现有配方 ID，创建后不可改
    recipe_ids  TEXT    NOT NULL,
    -- 上次求解时各配方的版本（{recipeId: recipeVersion}）
    recipe_versions_json TEXT NOT NULL DEFAULT '{}',
    version     INTEGER NOT NULL DEFAULT 1,
    -- 本次求解所用材料参数快照（与 result_json 严格同版）
    materials_json TEXT NOT NULL,
    solvable    INTEGER NOT NULL,
    result_json TEXT,        -- solvable=0 时为 NULL（无旧曲线）
    reason      TEXT,        -- solvable=0 时的中文原因
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS archives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id        INTEGER NOT NULL UNIQUE,
    draft_version   INTEGER NOT NULL,
    materials_json  TEXT    NOT NULL,  -- 冻结当时的材料参数
    result_json     TEXT    NOT NULL,  -- 冻结完整求解结果
    archived_at     TEXT    NOT NULL,
    FOREIGN KEY (draft_id) REFERENCES drafts(id)
);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn) -> None:
    """旧库就地升级：recipes 缺少 status 列时补上，既有配方默认可用。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)").fetchall()}
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE recipes ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )


@contextmanager
def connect():
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def lock():
    """全局读锁。所有涉及配方/草稿/归档的操作都在该锁内完成。"""
    return _LOCK


def write_lock():
    """写操作互斥锁。所有写路径（含配方重算的提交与兜底）都先取它、再取
    全局读锁；读操作不经过它，因此兜底路径求解期间读取照常返回已提交状态。"""
    return _WRITE_LOCK
