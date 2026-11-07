"""任务归档（阶段八）。

readme 阶段八要求「增加任务归档」。已完成（或已取消）的任务如果一直留在列表里，
时间一长列表就很难用；但直接删除又会丢掉打印过程的记录（哪些纸打过、哪些作废过、
补打了几轮），所以这里做的是**归档而不是删除**：

`Data/completed/archive/<归档日期>/<任务号>/` 下保存

- `summary.json`：任务信息、全部事件、批次、逐面打印记录、异常记录、打印命令记录；
- `source.pdf`：源 PDF 的副本（源文件找不到时跳过，并在结果里说明）；
- `pages/`：可选，逐页单页 PDF（`include_pages=True` 时），便于日后重打。

归档完成后在数据库里记一条 `task_archive` 事件；只有显式要求（`remove_jobs=True`）
才删除 `Data/jobs/<任务号>/`，默认不动任何原始文件。
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.core.models import Task
from app.core.task_status import CLOSED_STATUSES

logger = logging.getLogger("smartprint.services.archive")

#: 归档时写入的事件类型
EVENT_ARCHIVE = "task_archive"


class ArchiveError(RuntimeError):
    """归档失败。"""


@dataclass(frozen=True)
class ArchiveOutcome:
    """单个任务的归档结果。"""

    task_uid: str
    document_name: str
    ok: bool
    message: str
    directory: Path | None = None


@dataclass
class ArchiveBatchResult:
    """批量归档的结果。"""

    outcomes: list[ArchiveOutcome] = field(default_factory=list)

    @property
    def archived(self) -> list[ArchiveOutcome]:
        return [item for item in self.outcomes if item.ok]

    @property
    def failed(self) -> list[ArchiveOutcome]:
        return [item for item in self.outcomes if not item.ok]

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and not self.failed

    @property
    def summary_text(self) -> str:
        if not self.outcomes:
            return "没有需要归档的任务"
        parts = [f"已归档 {len(self.archived)} 个任务"]
        if self.failed:
            parts.append(f"失败 {len(self.failed)} 个")
        return "，".join(parts)


@dataclass(frozen=True)
class ArchivedTask:
    """已经归档到磁盘的任务（从 summary.json 读出来）。"""

    task_uid: str
    document_name: str
    status: str
    directory: Path
    archived_at: datetime
    page_count: int = 0
    has_source: bool = False
    has_pages: bool = False

    @property
    def archived_text(self) -> str:
        return self.archived_at.strftime("%Y-%m-%d %H:%M:%S")


def _task_to_dict(task: Task) -> dict:
    """把 Task 转成可写入 JSON 的字典（只取可读字段，避免依赖私有实现）。"""
    return {
        "task_uid": task.task_uid,
        "file_name": task.file_name,
        "document_name": task.document_name,
        "page_count": task.page_count,
        "paper_size": getattr(task, "paper_size", ""),
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "status_label": task.status_label,
        "source_path": str(task.source_path),
        "created_at": task.created_at.isoformat(timespec="seconds"),
        "updated_at": task.updated_at.isoformat(timespec="seconds"),
        "note": task.note,
    }


class ArchiveService:
    """把已完成任务的记录整理成一份可长期保存的目录。"""

    def __init__(self, repository, archive_root: Path, *, clock=None, jobs_dir: Path | None = None):
        self.repository = repository
        self.archive_root = Path(archive_root)
        self.jobs_dir = Path(jobs_dir) if jobs_dir else None
        self._clock = clock or datetime.now

    # -- 归档 ---------------------------------------------------------------

    @staticmethod
    def can_archive(task: Task) -> bool:
        """只有已结束的任务才归档（进行中的任务还要继续打印）。"""
        return task.status in CLOSED_STATUSES

    def archive_tasks(
        self,
        tasks: list[Task],
        *,
        include_pages: bool = False,
        remove_jobs: bool = False,
    ) -> ArchiveBatchResult:
        result = ArchiveBatchResult()
        for task in tasks:
            if not self.can_archive(task):
                result.outcomes.append(
                    ArchiveOutcome(
                        task_uid=task.task_uid,
                        document_name=task.document_name,
                        ok=False,
                        message=f"任务状态为「{task.status_label}」，只有已完成或已取消的任务才能归档",
                    )
                )
                continue
            try:
                outcome = self._archive_one(
                    task, include_pages=include_pages, remove_jobs=remove_jobs
                )
            except (OSError, ValueError) as exc:
                logger.exception("归档任务失败：%s", task.task_uid)
                outcome = ArchiveOutcome(
                    task_uid=task.task_uid,
                    document_name=task.document_name,
                    ok=False,
                    message=f"归档失败：{exc}",
                )
            result.outcomes.append(outcome)
        return result

    def _archive_one(self, task: Task, *, include_pages: bool, remove_jobs: bool) -> ArchiveOutcome:
        now = self._clock()
        directory = self.archive_root / f"{now:%Y%m%d}" / task.task_uid
        directory.mkdir(parents=True, exist_ok=True)

        notes: list[str] = []
        # 1. 源 PDF 副本
        has_source = False
        source = Path(task.source_path)
        if source.is_file():
            try:
                shutil.copy2(source, directory / f"source{source.suffix or '.pdf'}")
                has_source = True
            except OSError as exc:
                notes.append(f"源文件复制失败：{exc}")
        else:
            notes.append("源文件已不在原处，只保存记录")

        # 2. 单页文件（可选）
        has_pages = False
        pages_source = None
        if self.jobs_dir:
            candidate = self.jobs_dir / task.task_uid
            if candidate.is_dir():
                pages_source = candidate
        if include_pages and pages_source:
            target = directory / "pages"
            try:
                shutil.copytree(pages_source, target, dirs_exist_ok=True)
                has_pages = True
            except OSError as exc:
                notes.append(f"单页文件复制失败：{exc}")
        elif include_pages:
            notes.append("没有找到单页文件目录")

        # 3. 记录汇总
        summary = {
            "archived_at": now.isoformat(timespec="seconds"),
            "task": _task_to_dict(task),
            "events": [
                {
                    "created_at": event.created_at.isoformat(timespec="seconds"),
                    "event_type": event.event_type,
                    "message": event.message,
                }
                for event in self._safe(lambda: self.repository.list_events(task.task_uid, limit=10000), [])
            ],
            "batches": [
                {
                    "batch_index": batch.batch_index,
                    "sheet_count": batch.sheet_count,
                    "page_count": batch.page_count,
                    "page_files": [path.name for path in batch.page_files],
                }
                for batch in self._safe(lambda: self.repository.list_batches(task.task_uid), [])
            ],
            "duplex_faces": [
                {
                    "round_index": face.round_index,
                    "batch_index": face.batch_index,
                    "sheet_number": face.sheet_number,
                    "face": face.face,
                    "page_number": face.page_number,
                    "status": face.status,
                    "submitted_at": face.submitted_at.isoformat(timespec="seconds")
                    if face.submitted_at
                    else None,
                    "confirmed_at": face.confirmed_at.isoformat(timespec="seconds")
                    if face.confirmed_at
                    else None,
                    "note": face.note,
                }
                for face in self._safe(lambda: self.repository.list_duplex_faces(task.task_uid), [])
            ],
            "duplex_anomalies": [
                {
                    "round_index": anomaly.round_index,
                    "sheet_number": anomaly.sheet_number,
                    "face": anomaly.face,
                    "front_page": anomaly.front_page,
                    "back_page": anomaly.back_page,
                    "reason": anomaly.reason,
                    "action": anomaly.action,
                    "created_at": anomaly.created_at.isoformat(timespec="seconds"),
                    "resolved_round": anomaly.resolved_round,
                    "resolved_at": anomaly.resolved_at.isoformat(timespec="seconds")
                    if anomaly.resolved_at
                    else None,
                    "result": anomaly.result,
                }
                for anomaly in self._safe(
                    lambda: self.repository.list_duplex_anomalies(task.task_uid), []
                )
            ],
            "print_jobs": [
                {
                    "page_number": job.page_number,
                    "status": job.status,
                    "job_id": job.job_id,
                    "created_at": job.created_at.isoformat(timespec="seconds"),
                    "printer_name": job.printer_name,
                    "message": job.message,
                }
                for job in self._safe(lambda: self.repository.list_print_jobs(task.task_uid, limit=1000), [])
            ],
            "files": {
                "source": has_source,
                "pages": has_pages,
            },
            "notes": notes,
        }
        summary_path = directory / "summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        # 4. 数据库里留一条归档记录
        self._safe(
            lambda: self.repository.add_event(
                task.task_uid, EVENT_ARCHIVE, f"任务已归档到 {directory}"
            ),
            None,
        )

        # 5. 可选：删除单页文件目录（默认不删）
        if remove_jobs and pages_source:
            try:
                shutil.rmtree(pages_source)
                notes.append("已删除单页文件目录")
            except OSError as exc:
                notes.append(f"删除单页文件失败：{exc}")

        message = f"已归档到 {directory}"
        if notes:
            message += f"（{'；'.join(notes)}）"
        logger.info("归档任务 %s → %s", task.task_uid, directory)
        return ArchiveOutcome(
            task_uid=task.task_uid,
            document_name=task.document_name,
            ok=True,
            message=message,
            directory=directory,
        )

    @staticmethod
    def _safe(call, default):
        """读取数据库时出错不影响归档本身。"""
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - 归档要尽量把能拿到的都写下来
            logger.warning("归档时读取记录失败：%s", exc)
            return default

    # -- 查询 ---------------------------------------------------------------

    def list_archives(self, limit: int = 200) -> list[ArchivedTask]:
        """列出已经归档的任务（按归档时间倒序）。"""
        if not self.archive_root.is_dir():
            return []
        items: list[ArchivedTask] = []
        for summary_path in self.archive_root.glob("*/*/summary.json"):
            try:
                data = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("读取归档记录失败（%s）：%s", summary_path, exc)
                continue
            if not isinstance(data, dict):
                continue
            task = data.get("task") if isinstance(data.get("task"), dict) else {}
            try:
                archived_at = datetime.fromisoformat(str(data.get("archived_at", "")))
            except ValueError:
                archived_at = datetime.fromtimestamp(0)
            files = data.get("files") if isinstance(data.get("files"), dict) else {}
            items.append(
                ArchivedTask(
                    task_uid=str(task.get("task_uid") or summary_path.parent.name),
                    document_name=str(task.get("document_name") or ""),
                    status=str(task.get("status_label") or task.get("status") or ""),
                    directory=summary_path.parent,
                    archived_at=archived_at,
                    page_count=int(task.get("page_count") or 0),
                    has_source=bool(files.get("source")),
                    has_pages=bool(files.get("pages")),
                )
            )
        items.sort(key=lambda item: item.archived_at, reverse=True)
        return items[:limit]

    def summary_text(self, limit: int = 200) -> str:
        items = self.list_archives(limit)
        if not items:
            return f"还没有归档（目录 {self.archive_root}）"
        return f"已归档 {len(items)} 个任务，最近一次 {items[0].archived_text}"
