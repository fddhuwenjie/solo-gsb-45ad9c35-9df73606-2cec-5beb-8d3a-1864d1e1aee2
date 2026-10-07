"""SQLite 访问层：连接、建表与少量约定常量。

所有写操作都在单条事务中完成，并通过 BEGIN IMMEDIATE 在事务一开始就
获取 SQLite 的库级保留锁，从而把"读修订号 -> 校验 -> 写入"这一过程串行化，
保证发布定版时不会与原文改写交错、混入不同工作稿修订的数据。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

# 译文状态机：
#   submitted -> confirmed（确认所依据的原文版本必须为当前版本）
# 原文段一旦改版，该段此前 confirmed 的译文全部翻转为 stale（待复核）。
STATUS_SUBMITTED = "submitted"
STATUS_CONFIRMED = "confirmed"
STATUS_STALE = "stale"  # 已确认译文因原文改版而失效，等待复核

SCHEMA = """
CREATE TABLE IF NOT EXISTS manuscripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1,  -- 工作稿修订号，原文每次改版 +1
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id),
    position      INTEGER NOT NULL,           -- 段落在篇内的顺序（从 0 起）
    source_version INTEGER NOT NULL DEFAULT 1,-- 该段当前原文版本号
    source_text   TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id    INTEGER NOT NULL REFERENCES segments(id),
    version       INTEGER NOT NULL,           -- 该段的第几个原文版本（从 1 起）
    source_text   TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (segment_id, version)
);

CREATE TABLE IF NOT EXISTS translations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id      INTEGER NOT NULL REFERENCES segments(id),
    source_version  INTEGER NOT NULL,         -- 提交时所依据的原文版本
    target_text     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'submitted',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 同一段、同一依据版本的最新译文：业务上只保留一条，重复提交为"改写译文"。
CREATE UNIQUE INDEX IF NOT EXISTS ux_translations_segment_version
    ON translations(segment_id, source_version)
    WHERE status != 'stale';

-- 定版是不可变快照：整篇双语文本在发布瞬间序列化进 snapshot_json，
-- 之后无论工作稿如何改写或复核，旧定版按版号读取都不会变化。
CREATE TABLE IF NOT EXISTS publications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id),
    edition       INTEGER NOT NULL,           -- 定版版号（篇内从 1 起）
    revision      INTEGER NOT NULL,           -- 所定格的工作稿修订号
    title         TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (manuscript_id, edition)
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "/data/bilingual.db")


def connect(database: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 的同步依赖与同步端点可能由线程池中
    # 不同工作线程执行，但每条连接只在单个请求内使用（请求私有），并由
    # BEGIN IMMEDIATE / busy_timeout 保证多连接写入时的串行与等待，故安全。
    conn = sqlite3.connect(
        database or db_path(), timeout=30, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    # WAL 允许读写并发；配合 BEGIN IMMEDIATE 保证多个写者严格串行。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(database: str | None = None) -> None:
    conn = connect(database)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每个请求一条连接（读请求不需要事务）。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启立即写事务。

    BEGIN IMMEDIATE 拿到保留锁后，其他写者会在 busy_timeout 内等待，
    因此修订号校验与后续写入对同一稿件是原子且可串行化的。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
