"""任务仓储：tasks / task_events / task_batches / print_jobs / duplex_* 表的读写。

所有 SQL 都集中在这里，服务层和界面层只操作 Task / TaskEvent / TaskBatch /
PrintJob / DuplexSession / DuplexFace / DuplexAnomaly 等对象。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.core.duplex import DuplexAnomaly, DuplexFace, DuplexSession
from app.core.models import Task, TaskBatch, TaskEvent
from app.core.print_job import PrintJob
from app.core.task_status import TaskStatus, parse_status
from app.database.db import Database

logger = logging.getLogger("smartprint.database.task_repository")

_TASK_COLUMNS = """
    id, task_uid, source_path, file_name, document_name, source_job_id,
    file_size, file_hash, page_count, paper_size, orientation,
    width_pt, height_pt, status, note, created_at, updated_at
"""

_BATCH_COLUMNS = """
    id, task_uid, batch_index, total_batches, sheet_count,
    front_pages, back_pages, page_files, created_at
"""

_PRINT_JOB_COLUMNS = """
    id, task_uid, page_number, file_path, printer_name, status, command,
    exit_code, duration_ms, queue_before, queue_after, job_id, message, created_at
"""

_DUPLEX_SESSION_COLUMNS = """
    id, task_uid, phase, sheets_per_batch, total_sheets, total_batches,
    flip_confirmed_batch, flip_confirmed_group, current_round, current_batch,
    current_sheet, current_face, current_page, note, created_at, updated_at,
    finished_at
"""

_DUPLEX_FACE_COLUMNS = """
    id, task_uid, step_index, round_index, batch_index, sheet_number, face,
    page_number, status, job_id, submitted_at, confirmed_at, note
"""

_DUPLEX_ANOMALY_COLUMNS = """
    id, task_uid, round_index, sheet_number, face, front_page, back_page,
    reason, action, created_at, resolved_round, resolved_at, result
"""

#: 插入时使用的字段（不带 id）
_DUPLEX_SESSION_FIELDS = """
    task_uid, phase, sheets_per_batch, total_sheets, total_batches,
    flip_confirmed_batch, flip_confirmed_group, current_round, current_batch,
    current_sheet, current_face, current_page, note, created_at, updated_at,
    finished_at
"""

_DUPLEX_FACE_FIELDS = """
    task_uid, step_index, round_index, batch_index, sheet_number, face,
    page_number, status, job_id, submitted_at, confirmed_at, note
"""

_DUPLEX_ANOMALY_FIELDS = """
    task_uid, round_index, sheet_number, face, front_page, back_page,
    reason, action, created_at, resolved_round, resolved_at, result
"""

#: 允许排序的字段（白名单，避免把界面传入的字符串直接拼进 SQL）
SORTABLE_COLUMNS: dict[str, str] = {
    "task_uid": "task_uid",
    "file_name": "file_name COLLATE NOCASE",
    "document_name": "document_name COLLATE NOCASE",
    "page_count": "page_count",
    "paper_size": "paper_size",
    "status": "status",
    "created_at": "created_at",
    "updated_at": "updated_at",
}

DEFAULT_SORT = "created_at"


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，用户输入的 % 和 _ 应当按普通字符匹配。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        task_uid=row["task_uid"],
        source_path=Path(row["source_path"]),
        file_name=row["file_name"],
        document_name=row["document_name"],
        source_job_id=row["source_job_id"],
        file_size=row["file_size"],
        file_hash=row["file_hash"],
        page_count=row["page_count"],
        paper_size=row["paper_size"],
        orientation=row["orientation"] or "纵向",
        width_pt=row["width_pt"],
        height_pt=row["height_pt"],
        status=parse_status(row["status"]),
        note=row["note"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _parse_page_list(text: str | None) -> tuple[int, ...]:
    """把逗号分隔的页码文本还原成整数元组。"""
    if not text:
        return ()
    pages: list[int] = []
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            pages.append(int(chunk))
        except ValueError:
            logger.warning("批次页码字段包含无法识别的值：%r", chunk)
    return tuple(pages)


def _format_page_list(pages: Iterable[int]) -> str:
    return ",".join(str(int(page)) for page in pages)


def _parse_file_list(text: str | None) -> tuple[Path, ...]:
    """把逗号分隔的文件路径还原成 Path 元组（单页 PDF）。"""
    if not text:
        return ()
    return tuple(Path(part) for part in str(text).split(",") if part.strip())


def _format_file_list(paths: Iterable[Path]) -> str:
    return ",".join(str(path) for path in paths)


def _row_to_batch(row: sqlite3.Row) -> TaskBatch:
    return TaskBatch(
        id=row["id"],
        task_uid=row["task_uid"],
        batch_index=int(row["batch_index"]),
        total_batches=int(row["total_batches"]),
        sheet_count=int(row["sheet_count"]),
        front_pages=_parse_page_list(row["front_pages"]),
        back_pages=_parse_page_list(row["back_pages"]),
        page_files=_parse_file_list(row["page_files"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_print_job(row: sqlite3.Row) -> PrintJob:
    return PrintJob(
        id=row["id"],
        task_uid=row["task_uid"],
        page_number=int(row["page_number"]),
        file_path=Path(row["file_path"]),
        printer_name=row["printer_name"],
        status=row["status"],
        command=row["command"] or "",
        exit_code=row["exit_code"],
        duration_ms=int(row["duration_ms"] or 0),
        queue_before=row["queue_before"] or "",
        queue_after=row["queue_after"] or "",
        job_id=row["job_id"],
        message=row["message"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_duplex_session(row: sqlite3.Row) -> DuplexSession:
    return DuplexSession(
        id=row["id"],
        task_uid=row["task_uid"],
        phase=row["phase"],
        sheets_per_batch=int(row["sheets_per_batch"]),
        total_sheets=int(row["total_sheets"]),
        total_batches=int(row["total_batches"]),
        flip_confirmed_batch=int(row["flip_confirmed_batch"] or 0),
        flip_confirmed_group=row["flip_confirmed_group"] or "",
        current_round=int(row["current_round"] or 0),
        current_batch=int(row["current_batch"] or 1),
        current_sheet=row["current_sheet"],
        current_face=row["current_face"],
        current_page=row["current_page"],
        note=row["note"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        finished_at=_optional_datetime(row["finished_at"]),
    )


def _row_to_duplex_face(row: sqlite3.Row) -> DuplexFace:
    return DuplexFace(
        id=row["id"],
        task_uid=row["task_uid"],
        step_index=int(row["step_index"]),
        round_index=int(row["round_index"] or 0),
        batch_index=int(row["batch_index"]),
        sheet_number=int(row["sheet_number"]),
        face=row["face"],
        page_number=int(row["page_number"]),
        status=row["status"],
        job_id=row["job_id"],
        submitted_at=_optional_datetime(row["submitted_at"]),
        confirmed_at=_optional_datetime(row["confirmed_at"]),
        note=row["note"] or "",
    )


def _row_to_duplex_anomaly(row: sqlite3.Row) -> DuplexAnomaly:
    return DuplexAnomaly(
        id=row["id"],
        task_uid=row["task_uid"],
        round_index=int(row["round_index"] or 0),
        sheet_number=int(row["sheet_number"]),
        face=row["face"],
        front_page=row["front_page"],
        back_page=row["back_page"],
        reason=row["reason"] or "",
        action=row["action"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        resolved_round=row["resolved_round"],
        resolved_at=_optional_datetime(row["resolved_at"]),
        result=row["result"] or "",
    )


class TaskRepository:
    """打印任务的数据访问对象。"""

    def __init__(self, database: Database | None = None) -> None:
        self.database = database or Database()

    # -- 写入 ---------------------------------------------------------------

    def add_task(self, task: Task) -> Task:
        """插入任务；source_path 或 task_uid 重复时抛出 sqlite3.IntegrityError。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO tasks (
                    task_uid, source_path, file_name, document_name, source_job_id,
                    file_size, file_hash, page_count, paper_size, orientation,
                    width_pt, height_pt, status, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_uid,
                    str(task.source_path),
                    task.file_name,
                    task.document_name,
                    task.source_job_id,
                    task.file_size,
                    task.file_hash,
                    task.page_count,
                    task.paper_size,
                    task.orientation,
                    task.width_pt,
                    task.height_pt,
                    task.status.value,
                    task.note,
                    task.created_at.isoformat(timespec="seconds"),
                    task.updated_at.isoformat(timespec="seconds"),
                ),
            )
            task.id = int(cursor.lastrowid)
        logger.info(
            "任务已写入数据库：%s（%s，%s 页，%s）",
            task.task_uid,
            task.document_name,
            task.page_count,
            task.size_text,
        )
        return task

    def update_status(self, task_uid: str, status: TaskStatus | str) -> None:
        """更新任务状态（阶段三由任务管理服务调用）。"""
        new_status = parse_status(status)
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_uid = ?",
                (new_status.value, now, task_uid),
            )
        logger.info("任务 %s 状态变更为 %s", task_uid, new_status.label)

    def delete_task(self, task_uid: str) -> bool:
        """删除任务记录及其事件；返回是否真的删除了记录。

        注意：这里只负责数据库。源 PDF 文件由 TaskAdminService 处理，
        否则留在 incoming 目录里的文件会在下次扫描时被重新登记。
        """
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE task_uid = ?", (task_uid,))
            connection.execute("DELETE FROM task_events WHERE task_uid = ?", (task_uid,))
            connection.execute("DELETE FROM task_batches WHERE task_uid = ?", (task_uid,))
            connection.execute("DELETE FROM print_jobs WHERE task_uid = ?", (task_uid,))
            connection.execute("DELETE FROM duplex_faces WHERE task_uid = ?", (task_uid,))
            connection.execute("DELETE FROM duplex_sessions WHERE task_uid = ?", (task_uid,))
            connection.execute(
                "DELETE FROM duplex_anomalies WHERE task_uid = ?", (task_uid,)
            )
            deleted = cursor.rowcount > 0
        if deleted:
            logger.info("任务记录已删除：%s", task_uid)
        else:
            logger.warning("删除任务失败（任务不存在）：%s", task_uid)
        return deleted

    def add_event(self, task_uid: str, event_type: str, message: str) -> TaskEvent:
        """记录一条任务事件。"""
        event = TaskEvent(
            task_uid=task_uid,
            event_type=event_type,
            message=message,
            created_at=datetime.now(),
        )
        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO task_events (task_uid, event_type, message, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.task_uid,
                    event.event_type,
                    event.message,
                    event.created_at.isoformat(timespec="seconds"),
                ),
            )
            event.id = int(cursor.lastrowid)
        return event

    # -- 批次（阶段四） -----------------------------------------------------

    def replace_batches(self, task_uid: str, batches: Iterable[TaskBatch]) -> int:
        """用新的批次列表整体替换某个任务的批次记录，返回写入条数。

        重新生成打印文件时页面数量可能变化，所以先删除旧记录再整体写入；
        整个过程在同一个事务里，中途失败不会留下「写了一半」的批次。
        """
        items = list(batches)
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute("DELETE FROM task_batches WHERE task_uid = ?", (task_uid,))
            for batch in items:
                connection.execute(
                    """
                    INSERT INTO task_batches (
                        task_uid, batch_index, total_batches, sheet_count,
                        front_pages, back_pages, page_files, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_uid,
                        batch.batch_index,
                        batch.total_batches,
                        batch.sheet_count,
                        _format_page_list(batch.front_pages),
                        _format_page_list(batch.back_pages),
                        _format_file_list(batch.page_files),
                        batch.created_at.isoformat(timespec="seconds") if batch.created_at else now,
                    ),
                )
        logger.info("任务 %s 的打印批次已写入：%s 批", task_uid, len(items))
        return len(items)

    def clear_batches(self, task_uid: str) -> int:
        """删除某个任务的批次记录，返回删除条数。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM task_batches WHERE task_uid = ?", (task_uid,)
            )
            deleted = cursor.rowcount
        return int(deleted or 0)

    def list_batches(self, task_uid: str) -> list[TaskBatch]:
        """按批次序号列出某个任务的批次记录。"""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_BATCH_COLUMNS} FROM task_batches "
                "WHERE task_uid = ? ORDER BY batch_index ASC",
                (task_uid,),
            ).fetchall()
        return [_row_to_batch(row) for row in rows]

    def count_batches(self, task_uid: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM task_batches WHERE task_uid = ?", (task_uid,)
            ).fetchone()
        return int(row["n"])

    # -- 打印提交记录（阶段五） ----------------------------------------------

    def add_print_job(self, job: PrintJob) -> PrintJob:
        """记录一次页面打印提交，返回带数据库主键的记录。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO print_jobs (
                    task_uid, page_number, file_path, printer_name, status, command,
                    exit_code, duration_ms, queue_before, queue_after, job_id, message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.task_uid,
                    job.page_number,
                    str(job.file_path),
                    job.printer_name,
                    job.status,
                    job.command,
                    job.exit_code,
                    job.duration_ms,
                    job.queue_before,
                    job.queue_after,
                    job.job_id,
                    job.message,
                    job.created_at.isoformat(timespec="seconds"),
                ),
            )
            job.id = int(cursor.lastrowid)
        logger.info("打印提交已记录：%s", job.summary_text())
        return job

    def update_print_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        queue_before: str | None = None,
        queue_after: str | None = None,
        spool_job_id: int | None = None,
        message: str | None = None,
    ) -> None:
        """更新一条打印提交记录（只更新传入的字段）。"""
        fields: list[str] = []
        params: list[object] = []
        mapping = {
            "status": status,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "queue_before": queue_before,
            "queue_after": queue_after,
            "job_id": spool_job_id,
            "message": message,
        }
        for column, value in mapping.items():
            if value is None:
                continue
            fields.append(f"{column} = ?")
            params.append(value)
        if not fields:
            return
        params.append(int(job_id))
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE print_jobs SET {', '.join(fields)} WHERE id = ?", params
            )

    def get_print_job(self, job_id: int) -> PrintJob | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {_PRINT_JOB_COLUMNS} FROM print_jobs WHERE id = ?", (int(job_id),)
            ).fetchone()
        return _row_to_print_job(row) if row else None

    def list_print_jobs(
        self, task_uid: str | None = None, limit: int = 100
    ) -> list[PrintJob]:
        """按任务列出打印提交记录（最新的在前）。"""
        sql = f"SELECT {_PRINT_JOB_COLUMNS} FROM print_jobs"
        params: list[object] = []
        if task_uid:
            sql += " WHERE task_uid = ?"
            params.append(task_uid)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_print_job(row) for row in rows]

    def count_print_jobs(self, task_uid: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM print_jobs"
        params: list[object] = []
        if task_uid:
            sql += " WHERE task_uid = ?"
            params.append(task_uid)
        with self.database.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["n"])

    # -- 手动双面打印（阶段六） ----------------------------------------------

    def replace_duplex_session(
        self, session: DuplexSession, faces: Iterable[DuplexFace]
    ) -> DuplexSession:
        """整体替换某个任务的双面打印会话与全部打印面。

        重新开始时页面数、批次划分都可能变化，所以先删后写；整个过程在同一个
        事务里，中途失败不会留下「只写了一半」的打印面。
        """
        items = list(faces)
        now = datetime.now()
        session.updated_at = now
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM duplex_faces WHERE task_uid = ?", (session.task_uid,)
            )
            connection.execute(
                "DELETE FROM duplex_sessions WHERE task_uid = ?", (session.task_uid,)
            )
            # 重新开始时旧轮次的异常记录随会话一起清空；历史只保留在 task_events 里
            connection.execute(
                "DELETE FROM duplex_anomalies WHERE task_uid = ?", (session.task_uid,)
            )
            cursor = connection.execute(
                f"""
                INSERT INTO duplex_sessions (
                    {_DUPLEX_SESSION_FIELDS}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.task_uid,
                    session.phase,
                    int(session.sheets_per_batch),
                    int(session.total_sheets),
                    int(session.total_batches),
                    int(session.flip_confirmed_batch),
                    session.flip_confirmed_group,
                    int(session.current_round),
                    int(session.current_batch),
                    session.current_sheet,
                    session.current_face,
                    session.current_page,
                    session.note,
                    session.created_at.isoformat(timespec="seconds"),
                    session.updated_at.isoformat(timespec="seconds"),
                    session.finished_at.isoformat(timespec="seconds")
                    if session.finished_at
                    else None,
                ),
            )
            session.id = int(cursor.lastrowid)
            for face in items:
                connection.execute(
                    f"""
                    INSERT INTO duplex_faces (
                        {_DUPLEX_FACE_FIELDS}
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        face.task_uid,
                        int(face.step_index),
                        int(face.round_index),
                        int(face.batch_index),
                        int(face.sheet_number),
                        face.face,
                        int(face.page_number),
                        face.status,
                        face.job_id,
                        face.submitted_at.isoformat(timespec="seconds")
                        if face.submitted_at
                        else None,
                        face.confirmed_at.isoformat(timespec="seconds")
                        if face.confirmed_at
                        else None,
                        face.note,
                    ),
                )
        logger.info(
            "任务 %s 的双面打印会话已创建：%s 张纸，%s 批，%s 个打印面",
            session.task_uid,
            session.total_sheets,
            session.total_batches,
            len(items),
        )
        return session

    def get_duplex_session(self, task_uid: str) -> DuplexSession | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {_DUPLEX_SESSION_COLUMNS} FROM duplex_sessions WHERE task_uid = ?",
                (task_uid,),
            ).fetchone()
        return _row_to_duplex_session(row) if row else None

    def update_duplex_session(self, task_uid: str, **fields: object) -> None:
        """更新会话状态（只更新传入的字段）。"""
        allowed = {
            "phase",
            "flip_confirmed_batch",
            "flip_confirmed_group",
            "current_round",
            "current_batch",
            "current_sheet",
            "current_face",
            "current_page",
            "note",
            "finished_at",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values.setdefault("updated_at", datetime.now())
        assignments: list[str] = []
        params: list[object] = []
        for column, value in values.items():
            if isinstance(value, datetime):
                value = value.isoformat(timespec="seconds")
            assignments.append(f"{column} = ?")
            params.append(value)
        params.append(task_uid)
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE duplex_sessions SET {', '.join(assignments)} WHERE task_uid = ?",
                params,
            )

    def list_duplex_faces(
        self, task_uid: str, round_index: int | None = None
    ) -> list[DuplexFace]:
        """按打印顺序列出某个任务的全部打印面（可只看某一轮）。"""
        sql = f"SELECT {_DUPLEX_FACE_COLUMNS} FROM duplex_faces WHERE task_uid = ?"
        params: list[object] = [task_uid]
        if round_index is not None:
            sql += " AND round_index = ?"
            params.append(int(round_index))
        sql += " ORDER BY step_index ASC"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_duplex_face(row) for row in rows]

    def add_duplex_faces(self, faces: Iterable[DuplexFace]) -> int:
        """追加新一轮的打印面（补打时使用，不删除历史轮次）。"""
        items = list(faces)
        if not items:
            return 0
        with self.database.connect() as connection:
            for face in items:
                connection.execute(
                    f"""
                    INSERT INTO duplex_faces (
                        {_DUPLEX_FACE_FIELDS}
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        face.task_uid,
                        int(face.step_index),
                        int(face.round_index),
                        int(face.batch_index),
                        int(face.sheet_number),
                        face.face,
                        int(face.page_number),
                        face.status,
                        face.job_id,
                        face.submitted_at.isoformat(timespec="seconds")
                        if face.submitted_at
                        else None,
                        face.confirmed_at.isoformat(timespec="seconds")
                        if face.confirmed_at
                        else None,
                        face.note,
                    ),
                )
        logger.info("追加 %s 个打印面（第 %s 轮补打）", len(items), items[0].round_index)
        return len(items)

    def get_duplex_face(self, face_id: int) -> DuplexFace | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {_DUPLEX_FACE_COLUMNS} FROM duplex_faces WHERE id = ?",
                (int(face_id),),
            ).fetchone()
        return _row_to_duplex_face(row) if row else None

    def update_duplex_face(self, face_id: int, **fields: object) -> None:
        """更新一个打印面的状态（只更新传入的字段）。"""
        allowed = {"status", "job_id", "submitted_at", "confirmed_at", "note"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        assignments: list[str] = []
        params: list[object] = []
        for column, value in values.items():
            if isinstance(value, datetime):
                value = value.isoformat(timespec="seconds")
            assignments.append(f"{column} = ?")
            params.append(value)
        params.append(int(face_id))
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE duplex_faces SET {', '.join(assignments)} WHERE id = ?", params
            )

    def void_duplex_sheet(
        self,
        task_uid: str,
        sheet_number: int,
        reason: str = "",
        round_index: int | None = None,
    ) -> int:
        """把整张物理纸作废：正反两面的状态都改成 void，返回更新行数。

        正反两面一起作废是 readme 10.5 的硬性要求：不能只补打其中一面。
        round_index 给定时只作废该轮次的行（阶段七：补打中再次异常只影响本轮）。
        """
        now = datetime.now().isoformat(timespec="seconds")
        sql = (
            "UPDATE duplex_faces SET status = 'void', note = ? "
            "WHERE task_uid = ? AND sheet_number = ?"
        )
        params: list[object] = [reason, task_uid, int(sheet_number)]
        if round_index is not None:
            sql += " AND round_index = ?"
            params.append(int(round_index))
        with self.database.connect() as connection:
            cursor = connection.execute(sql, params)
            updated = cursor.rowcount
            connection.execute(
                "UPDATE duplex_sessions SET note = ?, updated_at = ? WHERE task_uid = ?",
                (reason, now, task_uid),
            )
        logger.info("任务 %s 第 %s 张纸已整张作废：%s", task_uid, sheet_number, reason)
        return int(updated or 0)

    # -- 异常记录（阶段七） ---------------------------------------------------

    def add_duplex_anomaly(self, anomaly: DuplexAnomaly) -> DuplexAnomaly:
        """记录一次异常（readme 7.6）。"""
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO duplex_anomalies (
                    {_DUPLEX_ANOMALY_FIELDS}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anomaly.task_uid,
                    int(anomaly.round_index),
                    int(anomaly.sheet_number),
                    anomaly.face,
                    anomaly.front_page,
                    anomaly.back_page,
                    anomaly.reason,
                    anomaly.action,
                    anomaly.created_at.isoformat(timespec="seconds"),
                    anomaly.resolved_round,
                    anomaly.resolved_at.isoformat(timespec="seconds")
                    if anomaly.resolved_at
                    else None,
                    anomaly.result,
                ),
            )
            anomaly.id = int(cursor.lastrowid)
        logger.info(
            "任务 %s 第 %s 张纸记录异常：%s（%s）",
            anomaly.task_uid,
            anomaly.sheet_number,
            anomaly.face,
            anomaly.reason,
        )
        return anomaly

    def list_duplex_anomalies(
        self,
        task_uid: str | None = None,
        *,
        limit: int | None = None,
    ) -> list[DuplexAnomaly]:
        """异常的异常记录。

        ``task_uid`` 为空时取全部任务（阶段八的异常总览用），
        此时按时间倒序并受 ``limit`` 限制；给定任务时按发生顺序正序。
        """
        sql = f"SELECT {_DUPLEX_ANOMALY_COLUMNS} FROM duplex_anomalies"
        params: list[object] = []
        if task_uid:
            sql += " WHERE task_uid = ?"
            params.append(task_uid)
            sql += " ORDER BY id ASC"
        else:
            sql += " ORDER BY id DESC"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_duplex_anomaly(row) for row in rows]

    def resolve_duplex_anomalies(
        self,
        task_uid: str,
        sheet_number: int,
        round_index: int,
        result: str = "补打完成",
    ) -> int:
        """补打成功后回填异常记录的「后续补打结果」。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE duplex_anomalies SET resolved_round = ?, resolved_at = ?, result = ? "
                "WHERE task_uid = ? AND sheet_number = ? AND resolved_at IS NULL",
                (int(round_index), now, result, task_uid, int(sheet_number)),
            )
            resolved = cursor.rowcount
        return int(resolved or 0)

    def count_duplex_anomalies(self, task_uid: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM duplex_anomalies"
        params: list[object] = []
        if task_uid:
            sql += " WHERE task_uid = ?"
            params.append(task_uid)
        with self.database.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["n"])

    def count_duplex_faces(self, task_uid: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM duplex_faces"
        params: list[object] = []
        if task_uid:
            sql += " WHERE task_uid = ?"
            params.append(task_uid)
        with self.database.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return int(row["n"])

    def delete_duplex_session(self, task_uid: str) -> int:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM duplex_faces WHERE task_uid = ?", (task_uid,))
            connection.execute(
                "DELETE FROM duplex_anomalies WHERE task_uid = ?", (task_uid,)
            )
            cursor = connection.execute(
                "DELETE FROM duplex_sessions WHERE task_uid = ?", (task_uid,)
            )
            deleted = cursor.rowcount
        return int(deleted or 0)

    # -- 查询 ---------------------------------------------------------------

    def list_tasks(
        self,
        statuses: Iterable[TaskStatus | str] | None = None,
        search: str = "",
        sort_by: str = DEFAULT_SORT,
        descending: bool = True,
        limit: int = 500,
    ) -> list[Task]:
        """按条件列出任务。

        - statuses：只返回这些状态的任务，None 表示不筛选。
        - search：按文档名、文件名、任务号做模糊匹配（不区分大小写）。
        - sort_by：SORTABLE_COLUMNS 中的字段名，其他值退回创建时间。
        """
        clauses: list[str] = []
        params: list[object] = []

        status_values = [parse_status(value).value for value in (statuses or [])]
        if status_values:
            placeholders = ", ".join("?" for _ in status_values)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_values)

        term = (search or "").strip()
        if term:
            pattern = f"%{_escape_like(term)}%"
            clauses.append(
                "(document_name LIKE ? ESCAPE '\\' OR file_name LIKE ? ESCAPE '\\' "
                "OR task_uid LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 3)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order_column = SORTABLE_COLUMNS.get(sort_by, SORTABLE_COLUMNS[DEFAULT_SORT])
        direction = "DESC" if descending else "ASC"
        params.append(max(1, int(limit)))

        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks{where} "
                f"ORDER BY {order_column} {direction}, id {direction} LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def count_by_status(self) -> dict[TaskStatus, int]:
        """各状态的任务数量（未出现过的状态补 0）。"""
        counts = {status: 0 for status in TaskStatus}
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[parse_status(row["status"])] += int(row["n"])
        return counts

    def get_task(self, task_uid: str) -> Task | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_uid = ?", (task_uid,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def exists_source_path(self, source_path: Path | str) -> bool:
        """同一路径是否已经登记过（防止重复登记同一个 PDF）。"""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE source_path = ? LIMIT 1", (str(source_path),)
            ).fetchone()
        return row is not None

    def exists_source_job_id(self, source_job_id: str | None) -> bool:
        """PDF24 的任务 ID 是否已经存在（用于识别重复提交）。"""
        if not source_job_id:
            return False
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE source_job_id = ? LIMIT 1", (source_job_id,)
            ).fetchone()
        return row is not None

    def exists_task_uid(self, task_uid: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM tasks WHERE task_uid = ? LIMIT 1", (task_uid,)
            ).fetchone()
        return row is not None

    def count_tasks(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
        return int(row["n"])

    def list_events(self, task_uid: str | None = None, limit: int = 200) -> list[TaskEvent]:
        """列出任务事件，可按任务号过滤。"""
        sql = "SELECT id, task_uid, event_type, message, created_at FROM task_events"
        params: list[object] = []
        if task_uid:
            sql += " WHERE task_uid = ?"
            params.append(task_uid)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            TaskEvent(
                id=row["id"],
                task_uid=row["task_uid"],
                event_type=row["event_type"],
                message=row["message"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
