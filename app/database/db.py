"""SQLite 数据库连接与建表。

设计要点（对应 readme「五、最重要的设计：打印任务状态机」）：

- 任务状态与打印进度必须落盘，程序意外退出后仍能恢复。
- 每次关键操作都写入 task_events，方便回溯「当时发生了什么」。
- 阶段五新增 ``print_jobs``：记录每一次「把某一页送去打印」的命令、
  退出码与提交前后的队列快照。它只证明「命令发给了哪台打印机」，
  **不证明纸张已经出纸**（readme 3.3）。

连接策略：每次操作打开一个短连接（WAL 模式），这样后台接收线程与界面线程
各自读写也不会互相阻塞，不需要自己维护锁。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.utils.paths import DATABASE_FILE

logger = logging.getLogger("smartprint.database.db")

SCHEMA_VERSION = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid        TEXT    NOT NULL UNIQUE,
    source_path     TEXT    NOT NULL UNIQUE,
    file_name       TEXT    NOT NULL,
    document_name   TEXT    NOT NULL,
    source_job_id   TEXT,
    file_size       INTEGER NOT NULL,
    file_hash       TEXT,
    page_count      INTEGER NOT NULL,
    paper_size      TEXT    NOT NULL,
    orientation     TEXT    NOT NULL DEFAULT '纵向',
    width_pt        REAL    NOT NULL DEFAULT 0,
    height_pt       REAL    NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'pending',
    note            TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_source_job ON tasks(source_job_id);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid    TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_uid, id);

-- 阶段四：每份 PDF 的打印批次（一条记录 = 一批纸张的页码与单页文件）
-- 注意 page_files 保存的是 page_001.pdf 这类**单页文件**路径（逗号分隔），
-- 不是「本批正面 PDF / 背面 PDF」——readme 要求每次只提交一个页面。
CREATE TABLE IF NOT EXISTS task_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid      TEXT    NOT NULL,
    batch_index   INTEGER NOT NULL,
    total_batches INTEGER NOT NULL,
    sheet_count   INTEGER NOT NULL,
    front_pages   TEXT    NOT NULL,
    back_pages    TEXT    NOT NULL DEFAULT '',
    page_files    TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    UNIQUE(task_uid, batch_index)
);

CREATE INDEX IF NOT EXISTS idx_task_batches_task ON task_batches(task_uid, batch_index);

-- 阶段五：每一次「把某一页送去打印」的提交记录
-- status 只描述命令发送结果（submitted / failed / timeout / blocked …），
-- 不代表纸张已经出纸；纸张是否完整由用户在阶段六确认。
CREATE TABLE IF NOT EXISTS print_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid     TEXT    NOT NULL,
    page_number  INTEGER NOT NULL,
    file_path    TEXT    NOT NULL,
    printer_name TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    command      TEXT    NOT NULL DEFAULT '',
    exit_code    INTEGER,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    queue_before TEXT    NOT NULL DEFAULT '',
    queue_after  TEXT    NOT NULL DEFAULT '',
    job_id       INTEGER,
    message      TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_print_jobs_task ON print_jobs(task_uid, id);
CREATE INDEX IF NOT EXISTS idx_print_jobs_created ON print_jobs(created_at);

-- 阶段六：手动双面打印的会话。一个任务最多一条记录，phase 表示流程走到哪一步。
-- 这里不保存「打印成功」：confirmed 只能来自用户在界面上确认实际纸张。
CREATE TABLE IF NOT EXISTS duplex_sessions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid             TEXT    NOT NULL UNIQUE,
    phase                TEXT    NOT NULL,
    sheets_per_batch     INTEGER NOT NULL,
    total_sheets         INTEGER NOT NULL,
    total_batches        INTEGER NOT NULL,
    flip_confirmed_batch INTEGER NOT NULL DEFAULT 0,
    flip_confirmed_group TEXT    NOT NULL DEFAULT '',
    current_round        INTEGER NOT NULL DEFAULT 0,
    current_batch        INTEGER NOT NULL DEFAULT 1,
    current_sheet        INTEGER,
    current_face         TEXT,
    current_page         INTEGER,
    note                 TEXT    NOT NULL DEFAULT '',
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    finished_at          TEXT
);

-- 阶段六：每一个打印面（一张纸的正面或背面）各一条记录。
-- 阶段七加入 round_index：0 是正常打印，N 是第 N 轮补打；同一次打印里
-- 一张纸可能有多份记录（每轮一份），旧轮次的作废行只作为审计记录保留。
-- status: pending / submitted / confirmed / void
-- submitted 只表示命令已交给 Windows；confirmed 才是用户确认完整出纸。
CREATE TABLE IF NOT EXISTS duplex_faces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid      TEXT    NOT NULL,
    step_index    INTEGER NOT NULL,
    round_index   INTEGER NOT NULL DEFAULT 0,
    batch_index   INTEGER NOT NULL,
    sheet_number  INTEGER NOT NULL,
    face          TEXT    NOT NULL,
    page_number   INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    job_id        INTEGER,
    submitted_at  TEXT,
    confirmed_at  TEXT,
    note          TEXT    NOT NULL DEFAULT '',
    UNIQUE(task_uid, step_index)
);

CREATE INDEX IF NOT EXISTS idx_duplex_faces_task ON duplex_faces(task_uid, step_index);
CREATE INDEX IF NOT EXISTS idx_duplex_faces_sheet ON duplex_faces(task_uid, sheet_number);
CREATE INDEX IF NOT EXISTS idx_duplex_faces_round ON duplex_faces(task_uid, round_index, sheet_number);

-- 阶段七：异常记录。readme 7.6 要求每次异常都能查到整张纸的来龙去脉：
-- 哪一轮、哪张纸、正反面页码、异常发生在哪一面、时间、处理方式、补打结果。
CREATE TABLE IF NOT EXISTS duplex_anomalies (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_uid       TEXT    NOT NULL,
    round_index    INTEGER NOT NULL DEFAULT 0,
    sheet_number   INTEGER NOT NULL,
    face           TEXT    NOT NULL,
    front_page     INTEGER,
    back_page      INTEGER,
    reason         TEXT    NOT NULL DEFAULT '',
    action         TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    resolved_round INTEGER,
    resolved_at    TEXT,
    result         TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_duplex_anomalies_task
    ON duplex_anomalies(task_uid, sheet_number, round_index);

CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """数据库门面：负责连接、建表与基本的元信息读写。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DATABASE_FILE

    # -- 连接 ---------------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开一个短连接，自动提交/回滚并关闭。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- 初始化 -------------------------------------------------------------

    def initialize(self) -> None:
        """建表（幂等），并记录 schema 版本。"""
        with self.connect() as connection:
            self._migrate_batches(connection)
            self._migrate_duplex(connection)
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT INTO schema_info(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        logger.info("数据库已就绪：%s（schema v%s）", self.db_path, SCHEMA_VERSION)

    @staticmethod
    def _migrate_batches(connection: sqlite3.Connection) -> None:
        """把旧版的批次表换成「单页文件」结构。

        v2 的 task_batches 保存 front_file / back_file（一整批的正面/反面 PDF），
        v3 改为 page_files（按页码命名的单页 PDF）。批次表里的数据全部可以由
        源 PDF 重新计算出来，所以这里直接删掉旧表重建，旧数据不会丢失任何
        不可恢复的内容（重新点一次「页面拆分」即可）。
        """
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_batches'"
        ).fetchone()
        if row is None:
            return
        columns = {
            info[1]
            for info in connection.execute("PRAGMA table_info(task_batches)").fetchall()
        }
        if "page_files" in columns:
            return
        logger.info("升级批次表结构（旧列：%s）", "、".join(sorted(columns)))
        connection.execute("DROP TABLE task_batches")

    @staticmethod
    def _migrate_duplex(connection: sqlite3.Connection) -> None:
        """阶段七：给双面打印的两张表补上「轮次」相关列。

        旧库（schema v5）里没有 round_index / flip_confirmed_group /
        current_round。这里先用 PRAGMA 检查列是否存在，再逐个 ALTER，
        已有数据默认按「正常轮次」处理，不需要重建表。
        """
        additions = {
            "duplex_sessions": {
                "flip_confirmed_group": "TEXT NOT NULL DEFAULT ''",
                "current_round": "INTEGER NOT NULL DEFAULT 0",
            },
            "duplex_faces": {
                "round_index": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in additions.items():
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ).fetchone()
            if row is None:
                # 全新数据库：建表语句里已经包含这些列
                continue
            existing = {
                info[1]
                for info in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, ddl in columns.items():
                if name in existing:
                    continue
                logger.info("升级 %s：新增列 %s", table, name)
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # -- 元信息 -------------------------------------------------------------

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_info WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row else 0
